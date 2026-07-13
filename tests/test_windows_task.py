import os

import pytest

from app.scheduler import windows_task


def test_install_includes_absolute_config_path(monkeypatch, tmp_path):
    monkeypatch.setattr(windows_task.platform, "system", lambda: "Windows")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class Result:
            returncode = 0
        return Result()

    monkeypatch.setattr(windows_task.subprocess, "run", fake_run)

    config_path = str(tmp_path / "config.json")
    windows_task.install("C:\\App\\tool.exe", "--import-orders", 15, "TestTask", config_path)

    tr_index = captured["cmd"].index("/TR")
    command = captured["cmd"][tr_index + 1]
    assert "--config" in command
    # Relative-looking input must be resolved to an absolute path -- this
    # is what makes the scheduled task immune to whatever working
    # directory Task Scheduler happens to run it with.
    assert os.path.abspath(config_path) in command
    assert "--import-orders" in command


def test_install_resolves_relative_config_path(monkeypatch, tmp_path):
    monkeypatch.setattr(windows_task.platform, "system", lambda: "Windows")
    monkeypatch.chdir(tmp_path)
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class Result:
            returncode = 0
        return Result()

    monkeypatch.setattr(windows_task.subprocess, "run", fake_run)

    windows_task.install("tool.exe", "--sync", 30, "TestTask2", "config.json")

    tr_index = captured["cmd"].index("/TR")
    command = captured["cmd"][tr_index + 1]
    assert str(tmp_path / "config.json") in command


def test_install_raises_off_windows(monkeypatch):
    monkeypatch.setattr(windows_task.platform, "system", lambda: "Linux")
    with pytest.raises(RuntimeError):
        windows_task.install("exe", "--sync", 10, "Task", "config.json")
