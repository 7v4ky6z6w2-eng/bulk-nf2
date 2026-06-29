"""Notifications téléphone : Telegram bot + ntfy.sh (fallback).

Appelé par le hub quand une pending_op passe en applied/failed (et notified=0).
"""

from __future__ import annotations

import json
import logging
import sqlite3

import requests

from hub.central_db import ops_to_notify, mark_notified

log = logging.getLogger("notify")

_TG_API = "https://api.telegram.org/bot%s/sendMessage"


class Notifier:
    def __init__(self, bot_token: str = "", chat_id: str = "",
                 ntfy_topic: str = "", store_names: dict | None = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.ntfy_topic = ntfy_topic
        self.store_names = store_names or {}

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

        if status == "applied":
            icon = "✅"
            verb = "importé" if op_type == "bdr_import" else "mis à jour"
            action = "BDR" if op_type == "bdr_import" else "Prix"
            return "%s %s %s au %s" % (icon, action, verb, name)
        else:
            icon = "❌"
            action = "BDR" if op_type == "bdr_import" else "Prix"
            err = (op.get("error_msg") or "raison inconnue")[:200]
            return "%s Échec %s au %s : %s" % (icon, action, name, err)

    def _send_telegram(self, text: str) -> bool:
        if not self.bot_token or not self.chat_id:
            return False
        try:
            r = requests.post(_TG_API % self.bot_token,
                              json={"chat_id": self.chat_id, "text": text},
                              timeout=10)
            return r.status_code == 200
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
            return r.status_code in (200, 201)
        except Exception as exc:  # noqa: BLE001
            log.warning("ntfy échoué : %s", exc)
            return False

    def send(self, text: str) -> bool:
        ok = self._send_telegram(text)
        if not ok:
            ok = self._send_ntfy(text)
        return ok

    def notify_pending(self, con: sqlite3.Connection | None = None) -> int:
        """Envoie les notifications pour les ops non encore notifiées. Renvoie le nombre envoyé."""
        if not self.enabled or con is None:
            return 0
        ops = ops_to_notify(con)
        sent = 0
        for op in ops:
            msg = self._format(op)
            if self.send(msg):
                mark_notified(con, op["id"])
                sent += 1
                log.info("Notifié : %s", msg)
            else:
                log.warning("Notification échouée pour op #%d", op["id"])
        return sent
