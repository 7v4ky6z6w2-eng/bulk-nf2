"""Register/remove recurring jobs via Windows Task Scheduler.

Preferred over an in-process timer: it survives the GUI being closed and
the machine rebooting, and doesn't hold a process running between runs.
The GUI's schedule toggles call install()/remove(); each task just
re-invokes this same executable with the mode's CLI flag (--sync,
--stock-sync, or --import-orders), each on its own independent schedule.
"""

import platform
import subprocess

TASK_NAME_SYNC = "ERP-WooCommerce-Sync"
TASK_NAME_STOCK_SYNC = "ERP-WooCommerce-StockSync"
TASK_NAME_ORDER_IMPORT = "ERP-WooCommerce-OrderImport"


def _require_windows():
    if platform.system() != "Windows":
        raise RuntimeError("Scheduled sync is only supported on Windows "
                            "(uses schtasks).")


def install(exe_path, cli_flag, interval_minutes, task_name):
    """Creates/replaces a Task Scheduler entry that runs
    '<exe_path> <cli_flag>' every 'interval_minutes' minutes."""
    _require_windows()
    command = f'"{exe_path}" {cli_flag}'
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
