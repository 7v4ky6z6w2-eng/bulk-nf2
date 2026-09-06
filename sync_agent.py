"""Agent de synchronisation — à lancer sur chaque poste (magasins 1, 2, 3).

Usage :
  python sync_agent.py --store-id 2 --once
  python sync_agent.py --store-id 2 --interval 900

Flags :
  --store-id N     : identifiant du magasin dans stores.json (obligatoire)
  --stores-path P  : chemin alternatif vers stores.json (défaut : auto)
  --state-path P   : fichier JSON de watermarks (défaut : agent_state_<N>.json)
  --once           : une seule exécution puis quitte (pour tests / Task Scheduler)
  --interval N     : secondes entre chaque cycle (défaut : 900 = 15 min)
  --log-level L    : DEBUG | INFO | WARNING (défaut : INFO)
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import threading
import time

# ─── path setup ────────────────────────────────────────────────────────────────
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from stores import StoreRegistry, StoresError, app_dir    # noqa: E402
from sync.state import StateManager                      # noqa: E402
from sync.pusher import HubClient, HubError              # noqa: E402
from sync.reader import FirebirdReader                   # noqa: E402
from sync.apply_ops import apply_op, ApplyError          # noqa: E402

log = logging.getLogger("agent")

# Vécu en pratique : une écriture Firebird bloquée (verrou jamais libéré, ex.
# Netfact2/PRIME ouvert avec une transaction en cours sur la même pièce) bloque
# apply_op() INDÉFINIMENT -- sans limite, ce cycle ne finit jamais, le
# Planificateur de tâches finit par tuer le processus (ExecutionTimeLimit), et
# le MÊME op, jamais acquitté, est retenté à l'identique à chaque cycle suivant
# pour toujours (jusqu'à intervention manuelle en base, déjà vécu). Ce délai
# transforme un blocage infini en un échec normal, acquitté au hub (visible
# dans /historique, notifié comme tout autre échec) au lieu de rester coincé.
APPLY_OP_TIMEOUT = 300  # secondes


def _apply_op_with_timeout(op: dict, local_kw: dict, timeout: int = APPLY_OP_TIMEOUT) -> None:
    """Comme apply_op(op, local_kw), mais abandonne après `timeout` secondes au
    lieu de bloquer indéfiniment. Le thread bloqué (s'il y en a un) est laissé
    en daemon : il ne sera jamais rejoint, mais ne retient pas non plus le
    processus en vie -- tué net (et sa connexion Firebird avec) dès que ce
    cycle se termine, au lieu d'attendre la limite de 10 min du Planificateur."""
    result: dict = {}

    def _run() -> None:
        try:
            apply_op(op, local_kw)
            result["ok"] = True
        except Exception as exc:  # noqa: BLE001 — relayé tel quel au thread appelant
            result["ok"] = False
            result["exc"] = exc

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(
            "Écriture bloquée depuis plus de %ds (verrou Firebird ? "
            "Netfact2/PRIME ouvert sur la même pièce ?)" % timeout)
    if not result.get("ok"):
        raise result["exc"]


# ─── cycle ─────────────────────────────────────────────────────────────────────

def run_cycle(store_id: int, registry: StoreRegistry, state: StateManager) -> None:
    """Un cycle complet : apply pending ops → push données → acquitter."""
    store = registry.get(store_id)
    hub_client = HubClient(
        base_url=registry.hub_url(),
        # Le hub accepte la clé API ou le code d'accès ; on envoie celle qui est
        # configurée (évite un 403 si seul access_code est renseigné).
        api_key=registry.hub_api_key or registry.access_code,
        store_id=store_id,
    )

    # ── 1. Récupérer et appliquer les ops en attente ──────────────────────────
    try:
        ops = hub_client.pending_ops()
    except HubError as exc:
        log.warning("Hub injoignable pour récupérer les ops : %s — cycle annulé.", exc)
        return

    local_kw = store.connect_kwargs(local=True)
    for op in ops:
        op_id = op["id"]
        op_type = op.get("op_type", "?")

        # Si un acquittement précédent a échoué après une écriture Firebird
        # réussie, le hub représente la même op (toujours 'pending'). Ne pas
        # la ré-exécuter (double BDR / double écriture) : juste ré-acquitter.
        if state.is_op_applied(op_id):
            log.info("Op #%d déjà appliquée localement — ré-acquittement.", op_id)
            try:
                hub_client.report_op(op_id, ok=True)
            except HubError as exc:
                log.warning("Impossible de ré-acquitter op #%d : %s", op_id, exc)
            continue

        log.info("Application op #%d (%s)…", op_id, op_type)
        try:
            _apply_op_with_timeout(op, local_kw)
            state.mark_op_applied(op_id)  # avant l'acquittement : source de vérité locale
            hub_client.report_op(op_id, ok=True)
            log.info("Op #%d appliquée avec succès.", op_id)
        except (ApplyError, TimeoutError) as exc:
            log.error("Op #%d échouée : %s", op_id, exc)
            try:
                hub_client.report_op(op_id, ok=False, error_msg=str(exc))
            except HubError as hub_exc:
                log.warning("Impossible d'acquitter l'échec de op #%d : %s", op_id, hub_exc)
        except HubError as exc:
            log.warning("Impossible d'acquitter op #%d : %s", op_id, exc)

    # ── 2. Ouvrir Firebird local ──────────────────────────────────────────────
    try:
        reader = FirebirdReader(local_kw)   # __init__ connects immediately
    except Exception as exc:  # noqa: BLE001
        log.warning("Connexion Firebird locale échouée : %s — push ignoré.", exc)
        return

    # ── 3. Démarrer session de sync ───────────────────────────────────────────
    try:
        session_id = hub_client.start_sync(store_name=store.name)
    except HubError as exc:
        log.warning("Impossible de démarrer session hub : %s — push ignoré.", exc)
        reader.close()
        return

    total_rows = 0
    error_msg: str | None = None

    try:
        watermarks = state.all_watermarks()
        new_watermarks: dict[str, str] = {}

        for table, rows in reader.read_all(since=watermarks):
            if not rows:
                continue
            pushed = hub_client.push(session_id, table, rows)
            total_rows += pushed
            log.info("  %-24s %d lignes poussées.", table, pushed)

            # Watermark : prendre la valeur max de DATEMODIF / DATEPIECE selon la table
            wm_col = _watermark_col(table)
            if wm_col and rows:
                new_wm = max((str(r.get(wm_col) or "") for r in rows if r.get(wm_col)), default="")
                if new_wm:
                    new_watermarks[table] = new_wm

        # Avancer les watermarks seulement si tout s'est bien passé
        for table, wm in new_watermarks.items():
            state.set_watermark(table, wm)
        state.mark_ok()
        state.save()

    except HubError as exc:
        error_msg = "Hub : %s" % exc
        log.error("Erreur hub pendant le push : %s", exc)
    except Exception as exc:  # noqa: BLE001
        error_msg = "Firebird : %s" % exc
        log.error("Erreur Firebird pendant le push : %s", exc)
    finally:
        reader.close()
        try:
            hub_client.finish_sync(
                session_id,
                rows=total_rows,
                status="ok" if error_msg is None else "error",
                error_msg=error_msg,
            )
        except HubError as exc:
            log.warning("finish_sync échoué : %s", exc)

    if error_msg is None:
        log.info("Cycle terminé — %d lignes totales.", total_rows)


def _watermark_col(table: str) -> str | None:
    return {
        "article": "datemodif",
        "tiers": "datemodif",
        "famille": None,
        "piece": "datepiece",
        "item": None,
        "depot": None,
        "type_piece": None,
        "mode_regl": None,
        "stock_snapshot": None,
        "tresorerie_snapshot": "snap_date",
    }.get(table)


# ─── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    # PrimeNFAgent.exe est construit --windowed (pas de console DU TOUT, plus
    # de fenêtre noire qui flashe toutes les 15 min à chaque déclenchement de
    # la tâche planifiée) : print()/input() n'ont donc PLUS aucun endroit où
    # s'afficher si quelqu'un double-clique l'exe par erreur dans
    # l'Explorateur (sys.stdout/stdin sont None dans ce cas). On utilise une
    # boîte de message Windows native à la place, qui marche sans console.
    if len(sys.argv) == 1:
        msg = (
            "Ce programme ne doit pas etre double-clique directement : il a "
            "besoin de savoir quel magasin synchroniser (--store-id).\n\n"
            "Normalement, install_agent.bat (ou setup_task.ps1) a deja cree "
            "une tache planifiee Windows qui le lance automatiquement toutes "
            "les 15 minutes -- vous n'avez rien a faire vous-meme.\n\n"
            "Pour tester manuellement, ouvrez une invite de commandes ici et "
            "tapez :\n    PrimeNFAgent.exe --store-id 2 --once"
        )
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "PrimeNF Agent", 0x40)  # MB_ICONINFORMATION
        except Exception:  # noqa: BLE001 — pas Windows (dev/tests) : imprimer si une console existe
            if sys.stdout is not None:
                print(msg)
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Agent de synchronisation PrimeNF")
    parser.add_argument("--store-id", type=int, required=True, help="ID du magasin")
    parser.add_argument("--stores-path", default=None, help="Chemin vers stores.json")
    parser.add_argument("--state-path", default=None, help="Fichier JSON watermarks")
    parser.add_argument("--once", action="store_true", help="Un seul cycle puis quitter")
    parser.add_argument("--interval", type=int, default=900, help="Secondes entre cycles")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    # Toujours écrire agent.log (à côté de l'exe) : construit --windowed (voir
    # plus haut), il n'y a plus AUCUNE console pour lire les logs — sans
    # fichier, toute erreur de synchro passerait complètement inaperçue.
    _log_dir = app_dir()
    _handlers = [logging.handlers.RotatingFileHandler(
        os.path.join(_log_dir, "agent.log"), maxBytes=5_000_000, backupCount=3,
        encoding="utf-8")]
    if sys.stderr is not None:
        _handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=_handlers,
    )

    # Charger la configuration
    try:
        registry = StoreRegistry.load(args.stores_path) if args.stores_path \
            else StoreRegistry.load()
        store = registry.get(args.store_id)
    except StoresError as exc:
        log.error("Erreur de configuration : %s", exc)
        sys.exit(1)

    log.info("Agent démarré — magasin : %s (id=%d)", store.name, store.id)

    state_path = args.state_path or ("agent_state_%d.json" % args.store_id)
    state = StateManager(state_path)

    if args.once:
        try:
            run_cycle(args.store_id, registry, state)
        except Exception as exc:  # noqa: BLE001
            # Construit --windowed (voir plus haut) : sans ce filet, une erreur
            # inattendue ici crasherait sans laisser AUCUNE trace nulle part
            # (pas de console pour afficher le traceback) -- le Planificateur
            # verrait juste "tâche terminée", sans savoir qu'elle a échoué.
            log.error("Erreur inattendue dans run_cycle : %s", exc, exc_info=True)
        return

    while True:
        try:
            run_cycle(args.store_id, registry, state)
        except Exception as exc:  # noqa: BLE001
            log.error("Erreur inattendue dans run_cycle : %s", exc, exc_info=True)
        log.debug("Prochain cycle dans %ds.", args.interval)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
