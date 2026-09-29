"""Local-only database path, diagnostics, backup, and restore helpers.

This module intentionally uses only the Python standard library so the macOS
runtime manager can import it before the web application is started.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_DATABASE_NAME = "lead_opportunity_finder.sqlite3"
IMPORTANT_TABLES = (
    "observed_job_offers",
    "commercial_relationships",
    "commercial_exclusions",
    "search_runs",
)


def resolve_database_url(value: str, project_root: Path) -> str:
    """Resolve a relative SQLite URL against project_root, never the process cwd."""
    if not value.startswith("sqlite:///") or value in {"sqlite:///:memory:", "sqlite://"}:
        return value
    raw_path, separator, query = value.removeprefix("sqlite:///").partition("?")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = project_root / path
    resolved = path.resolve()
    suffix = f"?{query}" if separator else ""
    return f"sqlite:///{resolved.as_posix()}{suffix}"


def sqlite_database_path(database_url: str) -> Optional[Path]:
    """Return the filesystem path for a canonical SQLite URL, if file-backed."""
    if not database_url.startswith("sqlite:///") or database_url in {"sqlite:///:memory:", "sqlite://"}:
        return None
    raw_path = database_url.removeprefix("sqlite:///").partition("?")[0]
    return Path(raw_path)


def check_integrity(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"Base SQLite introuvable : {path}")
    # The file existence check above prevents sqlite3 from creating a new,
    # misleading empty database when validating a path.
    connection = sqlite3.connect(path)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    status = str(result[0]) if result else "missing_result"
    if status != "ok":
        raise RuntimeError(f"Échec du contrôle d’intégrité SQLite : {status}")
    return status


def database_diagnostics(path: Path) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "path": str(path.resolve()),
        "exists": path.is_file(),
        "size_bytes": path.stat().st_size if path.is_file() else 0,
        "integrity": "missing",
        "schema_version": None,
        "counts": {},
    }
    if not path.is_file():
        return result
    connection = sqlite3.connect(path)
    try:
        integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
        result["integrity"] = str(integrity_row[0]) if integrity_row else "missing_result"
        result["schema_version"] = connection.execute("PRAGMA user_version").fetchone()[0]
        existing = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        result["counts"] = {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in IMPORTANT_TABLES
            if table in existing
        }
    finally:
        connection.close()
    return result


def backup_database(
    source: Path,
    backup_directory: Path,
    *,
    env_path: Optional[Path] = None,
    include_env: bool = False,
    now: Optional[datetime] = None,
    label: str = "backup",
) -> Dict[str, Optional[str]]:
    """Create a consistent, non-overwriting SQLite snapshot using Backup API."""
    check_integrity(source)
    if include_env and (env_path is None or not env_path.is_file()):
        raise FileNotFoundError("Le fichier .env demandé est introuvable.")
    backup_directory.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = backup_directory / f"lead_opportunity_finder.{label}-{stamp}.sqlite3"
    if destination.exists():
        raise FileExistsError(f"La sauvegarde existe déjà : {destination}")

    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    os.chmod(destination, 0o600)
    check_integrity(destination)

    env_backup: Optional[Path] = None
    if include_env:
        env_backup = backup_directory / f"lead_opportunity_finder.env-sensitive-{stamp}"
        if env_backup.exists():
            raise FileExistsError(f"La sauvegarde .env existe déjà : {env_backup}")
        shutil.copy2(env_path, env_backup)
        os.chmod(env_backup, 0o600)
    return {"database": str(destination), "environment": str(env_backup) if env_backup else None}


def restore_database(
    backup: Path,
    target: Path,
    backup_directory: Path,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, str]:
    """Validate and restore through a temp DB, preserving a safety snapshot."""
    check_integrity(backup)
    if not target.is_file():
        raise FileNotFoundError(f"Base SQLite actuelle introuvable : {target}")
    safety = backup_database(
        target,
        backup_directory,
        now=now,
        label="pre-restore",
    )["database"]

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.restore-{os.getpid()}.tmp")
    if temporary.exists():
        raise FileExistsError(f"Fichier temporaire de restauration déjà présent : {temporary}")
    source_connection = sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(temporary)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    os.chmod(temporary, 0o600)
    check_integrity(temporary)

    temporary.replace(target)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{target}{suffix}")
        if sidecar.exists():
            sidecar.unlink()
    check_integrity(target)
    return {"restored": str(target), "safety_backup": str(safety)}
