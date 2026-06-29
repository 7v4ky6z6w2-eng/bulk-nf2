# PrimeNF Hub — Accès multi-magasins Netfact2

Outil de synchronisation, tableau de bord et édition centralisée pour **3 magasins Netfact2 / Firebird 2.5.9** (bases `DIFA2.FDB`).

## Architecture

```
MAGASIN 1 (hub, toujours allumé)          MAGASIN 2 / 3 (fermés hors heures)
  DIFA2.FDB                                 DIFA2.FDB
  central.db (SQLite WAL)                   PrimeNFAgent.exe  ─── push données ──►
  hub_server.py (:5000)                     (Task Scheduler, 15 min)
  PrimeNFHub.exe (app bureau)  ◄────────── ◄── pull pending ops ──────────────────
  Tableau de bord web                       applique BDR / prix en local
        │
        └── Telegram / ntfy → votre téléphone (notification succès/échec)
```

Tous les postes sont reliés par **Tailscale** (VPN WireGuard gratuit, chaque PC obtient une IP `100.x.y.z`).

## Composants

| Fichier | Rôle |
|---|---|
| `hub_server.py` | Serveur Flask hub (magasin 1 uniquement) |
| `sync_agent.py` | Agent de synchronisation (tous les postes) |
| `prime_hub.py` | Application bureau PySide6 (multi-page) |
| `backfill_store1.py` | Chargement initial de la base hub |
| `verify.py` | Diagnostic : fdb, Firebird, hub, clé API |

## Installation

### Étape 1 — Configuration

```bash
cp stores.json.example stores.json
# Remplir : Tailscale IPs, chemins FDB, clé API, token Telegram
```

### Étape 2 — Magasin 1 (Hub, Python installé)

```bash
pip install -r requirements_hub.txt
python backfill_store1.py          # chargement initial
python hub_server.py               # ou via NSSM : install_windows\install_hub.bat
```

Tableau de bord web : `http://localhost:5000`

### Étape 3 — Application bureau (n'importe quel poste)

```bash
pip install -r requirements_desktop.txt
python prime_hub.py
```

### Étape 4 — Magasins 2 & 3 (sans Python)

Sur le poste de build (magasin 1) :

```bat
install_windows\build_windows.bat
```

Copier `dist\PrimeNFAgent.exe` + `stores.json` sur le poste cible, puis :

```bat
# En tant qu'Administrateur sur le poste cible :
install_windows\install_agent.bat 2    # pour le magasin 2
install_windows\install_agent.bat 3    # pour le magasin 3
```

Ou via PowerShell :

```powershell
powershell -ExecutionPolicy Bypass -File install_windows\setup_task.ps1 -StoreId 2
```

### Étape 5 — Vérification

```bash
python verify.py --store-id 1      # sur le hub
python verify.py --store-id 2      # sur le poste 2 (si Python dispo)
# PrimeNFAgent.exe --store-id 2 --once  # sur le poste 2 (exe)
```

## Notifications téléphone

Créez un bot Telegram via [@BotFather](https://t.me/botfather), renseignez `bot_token` et `chat_id` dans `stores.json`.  
Ou utilisez [ntfy.sh](https://ntfy.sh) : installez l'app, renseignez `ntfy_topic`.

Messages reçus :
- ✅ `BDR importé au Magasin Blida (12 articles)`
- ❌ `Échec Prix Magasin BEZ : connexion refusée`

## Application bureau — Pages

| Page | Description |
|---|---|
| Vue d'ensemble | État de connexion des 3 magasins |
| Stock | Stock agrégé (filtrable) |
| Ventes | Pièces des 7 derniers jours |
| Trésorerie | Encaissements du jour par mode |
| Import BDR | Choisir magasin → charger Excel → importer (direct ou file) |
| Éditeur de prix | Chercher article → nouveau prix → scope 1 ou 3 magasins |
| Synchronisation | Journal des sessions + file d'attente |

## Fonctionnement hors-ligne

- Le tableau de bord affiche toujours les **dernières données synchronisées** (avec "il y a Xh").
- Les écritures vers un magasin hors-ligne sont **mises en file** (`pending_ops`).
- À la prochaine connexion du poste, l'agent **applique les ops localement** et **envoie une notification**.

## Schéma des données clés (Netfact2)

- BDR : type `PC_AC_B`, `ANNULEE=1` = ACTIVE (stock incrémenté), NOPIECE via générateur `NEXTPIECE`
- Items : colonne `QTE` (pas `QTEUNIT`)
- Trésorerie : `PIECE.MONTANTVERSE` + `MODE_REGL`, `ANNULEE=0` pour les ventes
- Charset : `WIN1256` (arabe + français)
