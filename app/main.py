#!/usr/bin/env python3
"""Entry point.

No args (or double-clicking the .exe): launches the GUI.
--sync: runs one sync pass headlessly and exits -- this is what the
Windows Task Scheduler entry created by the GUI actually invokes.
"""

import argparse
import logging
import sys

from app.config import load_config

DEFAULT_CONFIG_PATH = "config.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description="ERP -> WooCommerce product sync")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Path to config.json")
    parser.add_argument("--sync", action="store_true", help="Run one sync pass headlessly and exit")
    parser.add_argument("--dry-run", action="store_true", help="With --sync: preview only, no writes")
    args = parser.parse_args(argv)

    if args.sync:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        from app.sync.engine import run_sync
        cfg = load_config(args.config)
        report = run_sync(cfg, dry_run=args.dry_run)
        if report["errors"]:
            return 1
        return 0

    from app.gui.main_window import launch
    return launch(args.config)


if __name__ == "__main__":
    sys.exit(main())
