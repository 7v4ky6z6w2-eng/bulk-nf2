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

# schtasks.exe's /TR argument has an undocumented ~262-character limit and
# fails with a generic, unhelpful error past it -- easy to hit once both
# the exe path and an absolute --config path are embedded in one command,
# especially under a deeply nested install folder (e.g. a Downloads
# directory with a "(11)" duplicate-download suffix). Routing through a
# tiny wrapper .bat in a short, fixed per-user folder keeps /TR itself
# short no matter how long the real paths are.
_WRAPPER_DIR_NAME = "ERP-WooCommerce-Sync"


def _require_windows():
    if platform.system() != "Windows":
        raise RuntimeError("Scheduled sync is only supported on Windows "
                            "(uses schtasks).")


def _wrapper_dir():
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, _WRAPPER_DIR_NAME)


def _wrapper_path(task_name):
    return os.path.join(_wrapper_dir(), f"{task_name}.bat")


def install(exe_path, cli_flag, interval_minutes, task_name, config_path):
    """Creates/replaces a Task Scheduler entry that runs a small wrapper
    .bat which in turn runs '<exe_path> <cli_flag> --config <config_path>'
    every 'interval_minutes' minutes.

    config_path is resolved to an absolute path: Task Scheduler runs
    actions with its own working directory (not the exe's folder, and not
    wherever the GUI happened to be launched from), so main.py's relative
    "config.json" default would silently fall back to DEFAULT_CONFIG's
    placeholder Firebird credentials/path -- which is exactly what caused
    a scheduled --import-orders run to fail with "Your user name and
    password are not defined" while the same action worked fine from the
    GUI. The absolute exe + config paths are written into the wrapper
    .bat's contents rather than passed directly to schtasks, so the /TR
    value itself (just the short wrapper path) never risks that length
    limit regardless of how deep the real install folder is."""
    _require_windows()
    abs_exe = os.path.abspath(exe_path)
    abs_config = os.path.abspath(config_path)

    wrapper_dir = _wrapper_dir()
    os.makedirs(wrapper_dir, exist_ok=True)
    wrapper_path = _wrapper_path(task_name)
    with open(wrapper_path, "w", encoding="utf-8") as fh:
        fh.write(f'@echo off\r\n"{abs_exe}" {cli_flag} --config "{abs_config}"\r\n')

    subprocess.run(
        ["schtasks", "/Create", "/TN", task_name, "/TR", f'"{wrapper_path}"',
         "/SC", "MINUTE", "/MO", str(int(interval_minutes)), "/F"],
        check=True, capture_output=True, text=True,
    )


def remove(task_name):
    _require_windows()
    subprocess.run(
        ["schtasks", "/Delete", "/TN", task_name, "/F"],
        check=False, capture_output=True, text=True,
    )
    try:
        os.remove(_wrapper_path(task_name))
    except OSError:
        pass


def is_installed(task_name):
    _require_windows()
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", task_name],
        capture_output=True, text=True,
    )
    return result.returncode == 0
