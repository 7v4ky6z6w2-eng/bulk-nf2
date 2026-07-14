import os

import pytest

from app.scheduler import windows_task


def _patch_run(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class Result:
            returncode = 0
        return Result()

    monkeypatch.setattr(windows_task.subprocess, "run", fake_run)
    return captured


def test_install_writes_wrapper_bat_with_full_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(windows_task.platform, "system", lambda: "Windows")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    captured = _patch_run(monkeypatch)

    exe_path = str(tmp_path / "deeply" / "nested" / "erp-woocommerce-sync.exe")
    config_path = str(tmp_path / "deeply" / "nested" / "config.json")

    windows_task.install(exe_path, "--import-orders", 15, "TestTask", config_path)

    wrapper_path = windows_task._wrapper_path("TestTask")
    assert os.path.isfile(wrapper_path)
    content = open(wrapper_path, encoding="utf-8").read()
    assert os.path.abspath(exe_path) in content
    assert os.path.abspath(config_path) in content
    assert "--import-orders" in content
    assert "--config" in content
    # cd's into the exe's own directory first, so every relative path the
    # app assumes (state_db_path, dry_run_payloads.json, ...) resolves the
    # same way it would from a normal double-click launch.
    exe_dir = os.path.dirname(os.path.abspath(exe_path))
    assert f'cd /d "{exe_dir}"' in content


def test_install_keeps_tr_argument_short_regardless_of_real_path_length(monkeypatch, tmp_path):
    # The bug this guards against: schtasks.exe's /TR argument has an
    # undocumented ~262-character limit and fails with a generic error
    # past it -- easy to hit once both an exe path and an absolute
    # --config path are embedded directly, especially under a deeply
    # nested install folder. /TR must stay short no matter how long the
    # real install path is, because the full paths now live inside the
    # wrapper .bat's contents instead.
    monkeypatch.setattr(windows_task.platform, "system", lambda: "Windows")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    captured = _patch_run(monkeypatch)

    long_component = "bulk-nf2-claude-woocommerce-product-sync-ql4z4w(11)"
    exe_path = str(tmp_path / "Downloads" / long_component / long_component / "dist" / "erp-woocommerce-sync.exe")
    config_path = str(tmp_path / "Downloads" / long_component / long_component / "dist" / "config.json")

    windows_task.install(exe_path, "--sync", 360, "TestTask2", config_path)

    tr_index = captured["cmd"].index("/TR")
    tr_value = captured["cmd"][tr_index + 1]
    assert len(tr_value) < 200
    assert tr_value == f'"{windows_task._wrapper_path("TestTask2")}"'


def test_remove_deletes_wrapper_bat(monkeypatch, tmp_path):
    monkeypatch.setattr(windows_task.platform, "system", lambda: "Windows")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _patch_run(monkeypatch)

    windows_task.install("exe.exe", "--sync", 30, "TestTask3", "config.json")
    wrapper_path = windows_task._wrapper_path("TestTask3")
    assert os.path.isfile(wrapper_path)

    windows_task.remove("TestTask3")
    assert not os.path.isfile(wrapper_path)


def test_remove_does_not_raise_when_wrapper_bat_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(windows_task.platform, "system", lambda: "Windows")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _patch_run(monkeypatch)
    windows_task.remove("NeverInstalledTask")  # must not raise


def test_install_raises_off_windows(monkeypatch):
    monkeypatch.setattr(windows_task.platform, "system", lambda: "Linux")
    with pytest.raises(RuntimeError):
        windows_task.install("exe", "--sync", 10, "Task", "config.json")
