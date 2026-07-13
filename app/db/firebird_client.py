"""Firebird connection helper.

Reuses the exact connection pattern proven by the existing
import_bon_reception.py script (same 'fdb' driver, same config shape), so
this tool talks to the ERP's .FDB file the same way that already-working
tool does.
"""

import os
import sys

import fdb

# Best-effort Firebird charset name -> Python codec name, so callers can
# pre-sanitize free-text values before they hit the wire: fdb raises rather
# than substituting when a string has a character the connection charset
# can't represent (e.g. a Kurdish/Persian letter in a customer name with a
# WIN1256 connection, which only covers Arabic).
_FB_CHARSET_TO_PYTHON = {
    "WIN1256": "cp1256",
    "WIN1252": "cp1252",
    "WIN1250": "cp1250",
    "UTF8": "utf-8",
    "ASCII": "ascii",
    "NONE": "ascii",
    "ISO8859_1": "iso8859-1",
    "DOS437": "cp437",
    "DOS850": "cp850",
}


def python_codec_for(fb_charset):
    return _FB_CHARSET_TO_PYTHON.get((fb_charset or "").upper(), "utf-8")


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
