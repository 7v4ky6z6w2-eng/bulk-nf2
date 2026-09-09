"""Notifications téléphone : Telegram bot + ntfy.sh (fallback).

Appelé par le hub quand une pending_op passe en applied/failed (et notified=0).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time

import requests

from hub.central_db import ops_to_notify, mark_notified

log = logging.getLogger("notify")

_TG_API = "https://api.telegram.org/bot%s/sendMessage"


class Notifier:
    def __init__(self, bot_token: str = "", chat_id: str = "",
                 ntfy_topic: str = "", store_names: dict | None = None,
                 retry_cooldown_sec: float = 60.0):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.ntfy_topic = ntfy_topic
        self.store_names = store_names or {}
        # Anti-spam : un canal en panne (mauvais token, ntfy.sh injoignable...)
        # ne doit pas ré-essayer un op à CHAQUE appel de notify_pending — celui-ci
        # est déclenché par report_op sur CHAQUE op terminée par un agent (bien
        # plus fréquent que la boucle de fond toutes les 2 min), donc sans ce
        # cooldown un canal cassé peut journaliser des dizaines de lignes
        # "Notification échouée" par minute pour le même op, indéfiniment tant
        # qu'il reste notified=0.
        self.retry_cooldown_sec = retry_cooldown_sec
        self._last_attempt: dict[int, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id) or bool(self.ntfy_topic)

    def _store_name(self, store_id: int) -> str:
        return self.store_names.get(store_id, "Magasin %d" % store_id)

    def _format(self, op: dict) -> str:
        sid = op["store_id"]
        name = self._store_name(sid)
        op_type = op["op_type"]
        status = op["status"]

        labels = {"bdr_import": ("BDR", "importé"),
                  "price_update": ("Prix", "mis à jour"),
                  "barcode_ops": ("Codes-barres", "mis à jour")}
        action, verb = labels.get(op_type, ("Opération", "appliquée"))
        if status == "applied":
            return "✅ %s %s au %s" % (action, verb, name)
        err = (op.get("error_msg") or "raison inconnue")[:200]
        return "❌ Échec %s au %s : %s" % (action, name, err)

    def _send_telegram(self, text: str) -> bool:
        if not self.bot_token or not self.chat_id:
            return False
        try:
            r = requests.post(_TG_API % self.bot_token,
                              json={"chat_id": self.chat_id, "text": text},
                              timeout=10)
            if r.status_code != 200:
                # Journalise la vraie cause (token/chat_id invalide, etc.) —
                # sans ça, un 4xx échoue silencieusement et seul l'appelant
                # log "Notification échouée", impossible à diagnostiquer.
                log.warning("Telegram échoué (HTTP %d) : %s",
                           r.status_code, r.text[:200])
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Telegram échoué : %s", exc)
            return False

    def _send_ntfy(self, text: str) -> bool:
        if not self.ntfy_topic:
            return False
        try:
            r = requests.post("https://ntfy.sh/" + self.ntfy_topic,
                              data=text.encode("utf-8"),
                              headers={"Content-Type": "text/plain; charset=utf-8"},
                              timeout=10)
            if r.status_code not in (200, 201):
                log.warning("ntfy échoué (HTTP %d) : %s", r.status_code, r.text[:200])
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("ntfy échoué : %s", exc)
            return False

    def send(self, text: str) -> bool:
        ok = self._send_telegram(text)
        if not ok:
            ok = self._send_ntfy(text)
        return ok

    def notify_pending(self, con: sqlite3.Connection | None = None) -> int:
        """Envoie les notifications pour les ops non encore notifiées. Renvoie le nombre envoyé.

        Appelé à la fois par la boucle de fond (toutes les 2 min) ET par
        report_op à CHAQUE op terminée par un agent — donc potentiellement
        plusieurs fois par seconde. Le cooldown par op évite qu'un canal en
        panne ne journalise en boucle pour le même op à chaque appel."""
        if not self.enabled or con is None:
            return 0
        ops = ops_to_notify(con)
        now = time.monotonic()
        sent = 0
        for op in ops:
            last = self._last_attempt.get(op["id"])
            if last is not None and (now - last) < self.retry_cooldown_sec:
                continue
            self._last_attempt[op["id"]] = now
            msg = self._format(op)
            if self.send(msg):
                mark_notified(con, op["id"])
                self._last_attempt.pop(op["id"], None)
                sent += 1
                log.info("Notifié : %s", msg)
            else:
                log.warning("Notification échouée pour op #%d (nouvelle tentative dans %ds)",
                           op["id"], int(self.retry_cooldown_sec))
        return sent
