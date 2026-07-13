#!/usr/bin/env python3
"""Entry point.

No args (or double-clicking the .exe): launches the GUI.
--sync / --stock-sync / --import-orders: run one pass of the
corresponding mode headlessly and exit -- these are what the Windows Task
Scheduler entries created by the GUI actually invoke, each on its own
schedule.
"""

import argparse
import logging
import os
import sys

from app.config import load_config

DEFAULT_CONFIG_PATH = "config.json"
DRY_RUN_PREVIEW_LIMIT = 15


def _require_config_file(path):
    """Headless modes (--sync/--stock-sync/--import-orders/--adopt) have no
    user watching to notice a silent fallback to DEFAULT_CONFIG's
    placeholder Firebird path/credentials -- that's exactly what made a
    Task-Scheduler-triggered run fail with a confusing Firebird login
    error while the same action worked fine from the GUI (Task Scheduler
    doesn't run with the exe's folder as the working directory, so the
    relative default "config.json" wasn't found). Fail loudly instead."""
    if not os.path.isfile(path):
        print(f"ERROR: config file not found: {path}\n"
              f"This mode needs a real, saved configuration. Run the GUI once, "
              f"fill in the Firebird/WooCommerce settings, and Save -- or check "
              f"that --config points at the right file.", file=sys.stderr)
        sys.exit(1)


def main(argv=None):
    parser = argparse.ArgumentParser(description="ERP -> WooCommerce product sync")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Path to config.json")
    parser.add_argument("--sync", action="store_true", help="Run one full product sync pass headlessly and exit")
    parser.add_argument("--stock-sync", action="store_true", help="Run one stock-only sync pass headlessly and exit")
    parser.add_argument("--import-orders", action="store_true", help="Run one order-import pass headlessly and exit")
    parser.add_argument("--adopt", action="store_true",
                        help="One-time: match existing WooCommerce products to DB articles by SKU and "
                             "record their ids (run with any 'hide products' plugin relaxed)")
    parser.add_argument("--dry-run", action="store_true", help="With --sync/--stock-sync/--import-orders: preview only, no writes")
    args = parser.parse_args(argv)

    if args.adopt:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        _require_config_file(args.config)
        from app.sync.reconcile import run_adopt
        cfg = load_config(args.config)
        run_adopt(cfg)
        return 0

    if args.sync:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        _require_config_file(args.config)
        from app.sync.engine import render_report_lines, run_sync, write_dry_run_payloads
        cfg = load_config(args.config)
        report = run_sync(cfg, dry_run=args.dry_run)

        # A catalog can run into the thousands of articles -- printing every
        # payload to the terminal isn't useful, so only a preview is shown
        # there and the full list is written to a JSON file for review.
        for line in render_report_lines(report, payload_limit=DRY_RUN_PREVIEW_LIMIT):
            print(line)
        written_path = write_dry_run_payloads(report)
        if written_path:
            print(f"\nFull list of {len(report['payloads'])} payload(s) written to {written_path}")

        if report["errors"]:
            return 1
        return 0

    if args.stock_sync:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        _require_config_file(args.config)
        from app.sync.stock_sync import run_stock_sync
        cfg = load_config(args.config)
        report = run_stock_sync(cfg, dry_run=args.dry_run)
        if args.dry_run:
            for u in report["updates_preview"][:DRY_RUN_PREVIEW_LIMIT]:
                print(f"[update] sku={u['sku']} stock_quantity={u['stock_quantity']}")
            remaining = len(report["updates_preview"]) - DRY_RUN_PREVIEW_LIMIT
            if remaining > 0:
                print(f"... and {remaining} more")
        return 1 if report["errors"] else 0

    if args.import_orders:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        _require_config_file(args.config)
        from app.sync.order_importer import run_order_import
        cfg = load_config(args.config)
        report = run_order_import(cfg, dry_run=args.dry_run)
        return 1 if report["errors"] else 0

    from app.gui.main_window import launch
    return launch(args.config)


if __name__ == "__main__":
    sys.exit(main())
