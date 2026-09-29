from datetime import datetime, timezone
import sqlite3

import pytest

from app.config import Settings
from app.local_operations import (
    backup_database,
    check_integrity,
    database_diagnostics,
    resolve_database_url,
    restore_database,
    sqlite_database_path,
)


NOW = datetime(2026, 9, 29, 8, 30, tzinfo=timezone.utc)


def _database(path, value="initial"):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    connection.execute("INSERT INTO marker VALUES (?)", (value,))
    connection.commit()
    connection.close()


def _marker(path):
    connection = sqlite3.connect(path)
    try:
        return connection.execute("SELECT value FROM marker").fetchone()[0]
    finally:
        connection.close()


def test_relative_sqlite_url_is_anchored_to_project_root_independent_of_cwd(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.chdir(tmp_path)
    first = resolve_database_url("sqlite:///data/app.sqlite3", project_root)
    monkeypatch.chdir(project_root)
    second = resolve_database_url("sqlite:///data/app.sqlite3", project_root)

    assert first == second == f"sqlite:///{project_root / 'data' / 'app.sqlite3'}"
    assert sqlite_database_path(first) == project_root / "data" / "app.sqlite3"


def test_settings_canonicalize_an_explicit_relative_sqlite_url():
    settings = Settings(database_url="sqlite:///data/custom.sqlite3", _env_file=None)
    assert sqlite_database_path(settings.database_url).is_absolute()
    assert settings.database_url.endswith("/data/custom.sqlite3")


def test_backup_and_restore_round_trip_with_integrity_and_safety_snapshot(tmp_path):
    database = tmp_path / "live.sqlite3"
    backups = tmp_path / "backups"
    _database(database)

    backup = backup_database(database, backups, now=NOW)
    backup_path = sqlite_database_path(f"sqlite:///{backup['database']}")
    assert backup_path is not None
    assert check_integrity(backup_path) == "ok"

    connection = sqlite3.connect(database)
    connection.execute("UPDATE marker SET value = 'changed'")
    connection.commit()
    connection.close()
    assert _marker(database) == "changed"

    restored = restore_database(backup_path, database, backups, now=NOW)
    assert _marker(database) == "initial"
    assert check_integrity(database) == "ok"
    assert check_integrity(sqlite_database_path(f"sqlite:///{restored['safety_backup']}")) == "ok"


def test_backup_never_overwrites_and_env_copy_is_explicit_and_private(tmp_path):
    database = tmp_path / "live.sqlite3"
    backups = tmp_path / "backups"
    env_file = tmp_path / ".env"
    _database(database)
    env_file.write_text("SECRET=local\n", encoding="utf-8")

    result = backup_database(database, backups, env_path=env_file, include_env=True, now=NOW)
    assert result["environment"] is not None
    assert oct((backups / result["environment"].split("/")[-1]).stat().st_mode & 0o777) == "0o600"
    with pytest.raises(FileExistsError):
        backup_database(database, backups, now=NOW)


def test_database_diagnostics_reports_absolute_identity_and_counts(tmp_path):
    database = tmp_path / "diagnostic.sqlite3"
    _database(database)
    diagnostic = database_diagnostics(database)
    assert diagnostic["path"] == str(database.resolve())
    assert diagnostic["exists"] is True
    assert diagnostic["size_bytes"] > 0
    assert diagnostic["integrity"] == "ok"
