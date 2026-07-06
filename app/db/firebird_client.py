"""Firebird connection helper.

Reuses the exact connection pattern proven by the existing
import_bon_reception.py script (same 'fdb' driver, same config shape), so
this tool talks to the ERP's .FDB file the same way that already-working
tool does.
"""

import os
import sys

import fdb


def _default_client_library_path():
    """Best-effort path to a bundled fbclient.dll next to a frozen (PyInstaller
    onefile) executable. Returns None when not running frozen or no such file
    is present -- fdb then falls back to normal OS library resolution."""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    candidate = os.path.join(base, "fbclient.dll")
    return candidate if os.path.isfile(candidate) else None


def connect(cfg):
    """Open a Firebird connection using the 'firebird' section of the app
    config (see config.DEFAULT_CONFIG for the shape)."""
    fb_cfg = cfg["firebird"]

    lib = fb_cfg.get("fb_client_library") or _default_client_library_path()
    if lib:
        fdb.load_api(lib)

    kwargs = dict(
        database=fb_cfg["database"],
        user=fb_cfg["user"],
        password=fb_cfg["password"],
        charset=fb_cfg.get("charset", "WIN1256"),
    )
    host = fb_cfg.get("host")
    if host:
        kwargs["host"] = host
        if fb_cfg.get("port"):
            kwargs["port"] = int(fb_cfg["port"])
    return fdb.connect(**kwargs)
