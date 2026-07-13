"""Register/remove recurring jobs via Windows Task Scheduler.

Preferred over an in-process timer: it survives the GUI being closed and
the machine rebooting, and doesn't hold a process running between runs.
The GUI's schedule toggles call install()/remove(); each task just
re-invokes this same executable with the mode's CLI flag (--sync,
--stock-sync, or --import-orders), each on its own independent schedule.
"""

import os
import platform
import subprocess

TASK_NAME_SYNC = "ERP-WooCommerce-Sync"
TASK_NAME_STOCK_SYNC = "ERP-WooCommerce-StockSync"
TASK_NAME_ORDER_IMPORT = "ERP-WooCommerce-OrderImport"


def _require_windows():
    if platform.system() != "Windows":
        raise RuntimeError("Scheduled sync is only supported on Windows "
                            "(uses schtasks).")


def install(exe_path, cli_flag, interval_minutes, task_name, config_path):
    """Creates/replaces a Task Scheduler entry that runs
    '<exe_path> <cli_flag> --config <config_path>' every 'interval_minutes'
    minutes.

    config_path is resolved to an absolute path before being embedded in
    the command: Task Scheduler runs actions with its own working
    directory (not the exe's folder, and not wherever the GUI happened to
    be launched from), so main.py's relative "config.json" default would
    silently fall back to DEFAULT_CONFIG's placeholder Firebird
    credentials/path -- which is exactly what caused a scheduled
    --import-orders run to fail with "Your user name and password are not
    defined" while the same action worked fine from the GUI."""
    _require_windows()
    abs_config = os.path.abspath(config_path)
    command = f'"{exe_path}" {cli_flag} --config "{abs_config}"'
    subprocess.run(
        ["schtasks", "/Create", "/TN", task_name, "/TR", command,
         "/SC", "MINUTE", "/MO", str(int(interval_minutes)), "/F"],
        check=True, capture_output=True, text=True,
    )


def remove(task_name):
    _require_windows()
    subprocess.run(
        ["schtasks", "/Delete", "/TN", task_name, "/F"],
        check=False, capture_output=True, text=True,
    )


def is_installed(task_name):
    _require_windows()
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", task_name],
        capture_output=True, text=True,
    )
    return result.returncode == 0
