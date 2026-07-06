"""Register/remove a recurring sync via Windows Task Scheduler.

Preferred over an in-process timer: it survives the GUI being closed and
the machine rebooting, and doesn't hold a process running between runs.
The GUI's "auto-sync every N hours" toggle calls install()/remove(); the
task itself just re-invokes this same executable with --sync.
"""

import platform
import subprocess

TASK_NAME = "ERP-WooCommerce-Sync"


def _require_windows():
    if platform.system() != "Windows":
        raise RuntimeError("Scheduled sync is only supported on Windows "
                            "(uses schtasks).")


def install(exe_path, interval_hours, task_name=TASK_NAME):
    """Creates/replaces a Task Scheduler entry that runs
    '<exe_path> --sync' every 'interval_hours' hours."""
    _require_windows()
    command = f'"{exe_path}" --sync'
    subprocess.run(
        ["schtasks", "/Create", "/TN", task_name, "/TR", command,
         "/SC", "HOURLY", "/MO", str(int(interval_hours)), "/F"],
        check=True, capture_output=True, text=True,
    )


def remove(task_name=TASK_NAME):
    _require_windows()
    subprocess.run(
        ["schtasks", "/Delete", "/TN", task_name, "/F"],
        check=False, capture_output=True, text=True,
    )


def is_installed(task_name=TASK_NAME):
    _require_windows()
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", task_name],
        capture_output=True, text=True,
    )
    return result.returncode == 0
