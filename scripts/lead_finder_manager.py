#!/usr/bin/env python3
"""Safe local process manager for Lead Opportunity Finder."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Optional, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
FRONTEND_INDEX = PROJECT_ROOT / "frontend" / "dist" / "index.html"
VENV_DIR = PROJECT_ROOT / ".venv"
VENV_PYTHON = VENV_DIR / "bin" / "python"
UVICORN = VENV_DIR / "bin" / "uvicorn"
ENV_FILE = PROJECT_ROOT / ".env"
DATA_DIR = PROJECT_ROOT / "data"
RUNTIME_DIR = DATA_DIR / "runtime"
BACKUP_DIR = DATA_DIR / "backups"
PID_FILE = RUNTIME_DIR / "lead_finder.pid.json"
LOCK_FILE = RUNTIME_DIR / "manager.lock"
BACKEND_LOG = RUNTIME_DIR / "backend.log"
MANAGER_LOG = RUNTIME_DIR / "manager.log"
HOST = "127.0.0.1"
PORT = 8000
APP_URL = f"http://{HOST}:{PORT}"
HEALTH_URL = f"{APP_URL}/api/v1/health"
HEALTH_MARKER = "local-v1"

sys.path.insert(0, str(BACKEND_DIR))
from app.local_operations import (  # noqa: E402
    backup_database,
    database_diagnostics,
    resolve_database_url,
    restore_database,
    sqlite_database_path,
)


class ManagerError(RuntimeError):
    pass


class ProcessInspectionError(ManagerError):
    """Raised when macOS prevents a reliable process ownership check."""


def _log(message: str) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if MANAGER_LOG.exists() and MANAGER_LOG.stat().st_size > 1_000_000:
        MANAGER_LOG.replace(MANAGER_LOG.with_suffix(".log.previous"))
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with MANAGER_LOG.open("a", encoding="utf-8") as stream:
        stream.write(f"[{timestamp}] {message}\n")


@contextmanager
def _manager_lock():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _dotenv_database_url() -> Optional[str]:
    if not ENV_FILE.is_file():
        return None
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if separator and key.strip() == "LEAD_FINDER_DATABASE_URL":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            return value
    return None


def database_path() -> Path:
    configured = os.environ.get("LEAD_FINDER_DATABASE_URL") or _dotenv_database_url()
    database_url = configured or "sqlite:///data/lead_opportunity_finder.sqlite3"
    canonical = resolve_database_url(database_url, PROJECT_ROOT)
    path = sqlite_database_path(canonical)
    if path is None:
        raise ManagerError("Le mode local nécessite une base SQLite persistante.")
    return path


def _port_is_used() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.25)
        return connection.connect_ex((HOST, PORT)) == 0


def _health_is_ours() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=1.0) as response:
            body = json.loads(response.read().decode("utf-8"))
            return (
                response.headers.get("X-Lead-Opportunity-Finder") == HEALTH_MARKER
                and body == {"status": "ok", "database": "connected"}
            )
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _process_detail(pid: int, field: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", f"{field}="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise ProcessInspectionError(
            f"Impossible d’inspecter le processus {pid} ; état de gestion conservé."
        ) from exc
    if result.returncode == 0:
        return result.stdout.strip() or None
    diagnostic = (result.stderr or "").strip().lower()
    if "operation not permitted" in diagnostic or "permission denied" in diagnostic:
        raise ProcessInspectionError(
            f"macOS refuse l’inspection du processus {pid} ; état de gestion conservé."
        )
    return None


def _process_start(pid: int) -> Optional[str]:
    return _process_detail(pid, "lstart")


def _process_command(pid: int) -> Optional[str]:
    return _process_detail(pid, "command")


def _read_pid_record() -> Optional[Dict]:
    try:
        value = json.loads(PID_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _owned_process(record: Optional[Dict] = None) -> Optional[Tuple[int, str]]:
    record = record or _read_pid_record()
    if not record:
        return None
    try:
        pid = int(record["pid"])
    except (KeyError, TypeError, ValueError):
        return None
    command = _process_command(pid)
    started = _process_start(pid)
    if not command or not started:
        return None
    if record.get("started") != started:
        return None
    if str(UVICORN) not in command or "app.main:app" not in command:
        return None
    return pid, command


def _remove_stale_pid_file() -> None:
    if PID_FILE.exists() and _owned_process() is None:
        PID_FILE.unlink()
        _log("PID file obsolète supprimé sans signaler de processus.")


def _validate_environment() -> None:
    missing = []
    for path, label in (
        (VENV_PYTHON, "Python de .venv"),
        (UVICORN, "uvicorn dans .venv"),
        (ENV_FILE, ".env local"),
        (FRONTEND_INDEX, "build frontend (frontend/dist/index.html)"),
    ):
        if not path.exists():
            missing.append(label)
    if missing:
        raise ManagerError("Prérequis manquants : " + ", ".join(missing) + ".")
    path = database_path()
    if not path.is_file():
        raise ManagerError(f"Base SQLite introuvable : {path}")


def _write_database_diagnostics() -> dict:
    diagnostics = database_diagnostics(database_path())
    _log("Diagnostic DB : " + json.dumps(diagnostics, ensure_ascii=False, sort_keys=True))
    return diagnostics


def start() -> str:
    with _manager_lock():
        _remove_stale_pid_file()
        owned = _owned_process()
        if owned:
            if _health_is_ours():
                return "Lead Opportunity Finder est déjà lancé."
            raise ManagerError(
                "Le processus géré existe mais n’est pas prêt ; aucun second processus n’a été lancé."
            )
        if _health_is_ours():
            _log("Instance compatible détectée sans PID file ; réutilisation sans prise de propriété.")
            return "Une instance Lead Opportunity Finder existante a été réutilisée."
        if _port_is_used():
            raise ManagerError(
                f"Le port {PORT} est utilisé par une autre application. Aucun processus n’a été arrêté."
            )
        _validate_environment()
        diagnostics = _write_database_diagnostics()
        if diagnostics["integrity"] != "ok":
            raise ManagerError("La base SQLite n’a pas passé le contrôle d’intégrité.")

        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        with BACKEND_LOG.open("wb") as log_stream:
            process = subprocess.Popen(
                [
                    str(UVICORN),
                    "app.main:app",
                    "--host",
                    HOST,
                    "--port",
                    str(PORT),
                    "--no-access-log",
                ],
                cwd=BACKEND_DIR,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        started = None
        for _ in range(20):
            started = _process_start(process.pid)
            if started:
                break
            time.sleep(0.05)
        if not started:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            raise ManagerError("Impossible d’identifier le processus local démarré.")
        PID_FILE.write_text(
            json.dumps({"pid": process.pid, "started": started, "port": PORT}, indent=2),
            encoding="utf-8",
        )
        os.chmod(PID_FILE, 0o600)

        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if process.poll() is not None:
                PID_FILE.unlink(missing_ok=True)
                raise ManagerError(f"Lead Opportunity Finder n’a pas pu démarrer. Consultez {BACKEND_LOG}")
            if _health_is_ours():
                _log(f"Application démarrée avec le PID {process.pid}.")
                return "Lead Opportunity Finder est prêt."
            time.sleep(0.25)
        if _owned_process():
            process.terminate()
        PID_FILE.unlink(missing_ok=True)
        raise ManagerError(f"Délai de démarrage dépassé. Consultez {BACKEND_LOG}")


def stop() -> str:
    with _manager_lock():
        record = _read_pid_record()
        owned = _owned_process(record)
        if owned is None:
            _remove_stale_pid_file()
            if _health_is_ours():
                raise ManagerError(
                    "L’instance répond, mais elle n’appartient pas à ce gestionnaire ; aucun signal n’a été envoyé."
                )
            return "Lead Opportunity Finder est déjà arrêté."
        pid, _ = owned
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if _process_command(pid) is None:
                PID_FILE.unlink(missing_ok=True)
                _log(f"Application arrêtée proprement (PID {pid}).")
                return "Lead Opportunity Finder est arrêté."
            time.sleep(0.2)
        # The exact PID, start time and command are revalidated before escalation.
        if _owned_process(record) is None:
            PID_FILE.unlink(missing_ok=True)
            return "Lead Opportunity Finder est arrêté."
        os.kill(pid, signal.SIGKILL)
        PID_FILE.unlink(missing_ok=True)
        _log(f"Application forcée à s’arrêter après timeout (PID {pid}).")
        return "Lead Opportunity Finder a été arrêté après un délai anormal."


def status() -> dict:
    _remove_stale_pid_file()
    owned = _owned_process()
    healthy = _health_is_ours()
    return {
        "running": healthy,
        "managed": owned is not None,
        "pid": owned[0] if owned else None,
        "port": PORT,
        "port_in_use": _port_is_used(),
        "url": APP_URL,
        "database": database_diagnostics(database_path()),
        "log": str(BACKEND_LOG),
    }


def create_backup(include_env: bool) -> dict:
    result = backup_database(
        database_path(),
        BACKUP_DIR,
        env_path=ENV_FILE,
        include_env=include_env,
    )
    _log("Sauvegarde créée : " + json.dumps(result, ensure_ascii=False))
    return result


def restore(backup_path: Path) -> dict:
    current = status()
    if current["managed"]:
        stop()
    elif current["running"]:
        raise ManagerError(
            "L’application active n’appartient pas au gestionnaire. Arrêtez-la manuellement avant restauration."
        )
    elif current["port_in_use"]:
        raise ManagerError(f"Le port {PORT} est occupé ; restauration annulée par sécurité.")
    with _manager_lock():
        result = restore_database(backup_path.resolve(), database_path(), BACKUP_DIR)
        _log("Restauration effectuée : " + json.dumps(result, ensure_ascii=False))
    start_message = start()
    result["startup"] = start_message
    result["diagnostics"] = database_diagnostics(database_path())
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("start")
    subparsers.add_parser("stop")
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--quiet", action="store_true")
    subparsers.add_parser("open")
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("--include-env", action="store_true")
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("backup", type=Path)
    subparsers.add_parser("db-info")
    args = parser.parse_args()
    try:
        if args.command == "start":
            print(start())
        elif args.command == "stop":
            print(stop())
        elif args.command == "status":
            value = status()
            if not args.quiet:
                print(json.dumps(value, ensure_ascii=False, indent=2))
            return 0 if value["running"] else 1
        elif args.command == "open":
            if not status()["running"]:
                start()
            webbrowser.open(APP_URL)
        elif args.command == "backup":
            print(json.dumps(create_backup(args.include_env), ensure_ascii=False, indent=2))
        elif args.command == "restore":
            print(json.dumps(restore(args.backup), ensure_ascii=False, indent=2, default=str))
        elif args.command == "db-info":
            print(json.dumps(database_diagnostics(database_path()), ensure_ascii=False, indent=2))
        return 0
    except (ManagerError, FileNotFoundError, FileExistsError, RuntimeError, OSError) as exc:
        _log(f"Erreur : {exc}")
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
