from pathlib import Path
import subprocess
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import lead_finder_manager as manager  # noqa: E402


def test_process_detail_reports_a_missing_process(monkeypatch):
    result = subprocess.CompletedProcess([], 1, stdout="", stderr="")
    monkeypatch.setattr(manager.subprocess, "run", lambda *args, **kwargs: result)

    assert manager._process_detail(123, "command") is None


def test_process_detail_does_not_treat_permission_denial_as_a_missing_process(monkeypatch):
    result = subprocess.CompletedProcess(
        [], 1, stdout="", stderr="ps: operation not permitted\n"
    )
    monkeypatch.setattr(manager.subprocess, "run", lambda *args, **kwargs: result)

    with pytest.raises(manager.ProcessInspectionError, match="état de gestion conservé"):
        manager._process_detail(123, "command")


def test_stale_pid_file_is_preserved_when_process_inspection_is_denied(tmp_path, monkeypatch):
    pid_file = tmp_path / "lead_finder.pid.json"
    pid_file.write_text('{"pid": 123}', encoding="utf-8")
    monkeypatch.setattr(manager, "PID_FILE", pid_file)

    def denied():
        raise manager.ProcessInspectionError("inspection refusée")

    monkeypatch.setattr(manager, "_owned_process", denied)

    with pytest.raises(manager.ProcessInspectionError):
        manager._remove_stale_pid_file()
    assert pid_file.exists()


def test_start_refuses_a_second_process_when_managed_process_is_not_ready(monkeypatch):
    monkeypatch.setattr(manager, "_remove_stale_pid_file", lambda: None)
    monkeypatch.setattr(manager, "_owned_process", lambda: (123, "managed uvicorn"))
    monkeypatch.setattr(manager, "_health_is_ours", lambda: False)

    with pytest.raises(manager.ManagerError, match="aucun second processus"):
        manager.start()


def test_restore_stops_a_managed_process_even_if_health_is_degraded(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        manager,
        "status",
        lambda: {"running": False, "managed": True, "port_in_use": False},
    )
    monkeypatch.setattr(manager, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(
        manager,
        "restore_database",
        lambda *args: {"restored": "copy", "safety_backup": "safety"},
    )
    monkeypatch.setattr(manager, "database_path", lambda: tmp_path / "live.sqlite3")
    monkeypatch.setattr(manager, "start", lambda: "ready")
    monkeypatch.setattr(manager, "database_diagnostics", lambda path: {"path": str(path)})

    result = manager.restore(tmp_path / "backup.sqlite3")

    assert calls == ["stop"]
    assert result["startup"] == "ready"
