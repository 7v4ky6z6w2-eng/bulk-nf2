import os
import tempfile

import pytest

from app import main as main_module


def test_require_config_file_passes_when_file_exists(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{}")
    main_module._require_config_file(str(path))  # must not raise


def test_require_config_file_exits_loudly_when_missing(tmp_path, capsys):
    missing = tmp_path / "does_not_exist.json"
    with pytest.raises(SystemExit) as exc_info:
        main_module._require_config_file(str(missing))
    assert exc_info.value.code == 1
    assert "config file not found" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--sync", "--stock-sync", "--import-orders", "--adopt"])
def test_headless_modes_exit_loudly_when_config_missing(flag, capsys):
    # A scheduled-task run with a config path that doesn't resolve (e.g. a
    # relative default under a working directory Task Scheduler doesn't
    # set to the exe's folder) must fail loudly instead of silently
    # running against DEFAULT_CONFIG's placeholder Firebird credentials.
    with tempfile.TemporaryDirectory() as tmp:
        missing = os.path.join(tmp, "does_not_exist.json")
        with pytest.raises(SystemExit) as exc_info:
            main_module.main([flag, "--config", missing])
        assert exc_info.value.code == 1
        assert "config file not found" in capsys.readouterr().err
