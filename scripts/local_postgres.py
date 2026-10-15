"""Manage an isolated, password-protected PostgreSQL instance for local development."""

import argparse
import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL, make_url

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / ".local" / "postgres"
DATA = DIRECTORY / "data"
CONFIG = DIRECTORY / "settings.json"
PORT = 55432


def binary_directory():
    executable = "pg_ctl.exe" if os.name == "nt" else "pg_ctl"
    candidates = [
        Path(os.environ.get("ANYSHIP_POSTGRES_BIN", "")),
        ROOT / ".local" / "pgsql" / "bin",
    ]
    if found := shutil.which(executable):
        candidates.append(Path(found).parent)
    if os.name == "nt":
        candidates.extend(sorted(Path("C:/Program Files/PostgreSQL").glob("*/bin"), reverse=True))
    for candidate in candidates:
        if (candidate / executable).is_file():
            return candidate.resolve()
    raise RuntimeError(
        "PostgreSQL binaries missing. See docs/LOCAL_DEVELOPMENT.md or set ANYSHIP_POSTGRES_BIN."
    )


def run(binary, *args, check=True):
    suffix = ".exe" if os.name == "nt" else ""
    result = subprocess.run(
        [str(binary_directory() / (binary + suffix)), *map(str, args)],
        # A background postgres process can inherit pipe handles on Windows,
        # keeping communicate() blocked after pg_ctl has already exited.
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        timeout=120,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if check and result.returncode:
        # Never print connection strings, credentials, or server statement logs.
        raise RuntimeError(
            f"{binary} failed (exit {result.returncode}). Check .local/postgres/server.log locally."
        )
    return result.returncode


def settings():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def database_url(database="anyship"):
    values = settings()
    return URL.create(
        "postgresql+psycopg",
        username="anyship",
        password=values["password"],
        host="127.0.0.1",
        port=PORT,
        database=database,
    ).render_as_string(hide_password=False)


def is_running():
    return (DATA / "PG_VERSION").exists() and run("pg_ctl", "-D", DATA, "status", check=False) == 0


def start():
    binary_directory()  # Fail before creating configuration if binaries are missing.
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    if not CONFIG.exists():
        if DATA.exists():
            raise RuntimeError(
                "Existing PostgreSQL data has no matching local configuration; refusing to replace it."
            )
        with CONFIG.open("x", encoding="utf-8") as stream:
            json.dump(
                {
                    "admin_password": secrets.token_urlsafe(32),
                    "password": secrets.token_urlsafe(32),
                },
                stream,
            )
    values = settings()
    if not (DATA / "PG_VERSION").exists():
        password_file = DIRECTORY / "initdb-password.tmp"
        try:
            password_file.write_text(values["admin_password"] + "\n", encoding="utf-8")
            run(
                "initdb",
                "-D",
                DATA,
                "-U",
                "anyship_admin",
                "--pwfile",
                password_file,
                "--auth=scram-sha-256",
                "--encoding=UTF8",
                "--locale=C",
            )
        finally:
            password_file.unlink(missing_ok=True)
    if not is_running():
        run(
            "pg_ctl",
            "-D",
            DATA,
            "-l",
            DIRECTORY / "server.log",
            "-o",
            f"-h 127.0.0.1 -p {PORT}",
            "-w",
            "-t",
            "30",
            "start",
        )
    with psycopg.connect(
        host="127.0.0.1",
        port=PORT,
        user="anyship_admin",
        password=values["admin_password"],
        dbname="postgres",
        connect_timeout=5,
        autocommit=True,
    ) as connection:
        if not connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", ("anyship",)
        ).fetchone():
            connection.execute(
                sql.SQL(
                    "CREATE ROLE anyship LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}"
                ).format(sql.Literal(values["password"]))
            )
        for name in ("anyship", "anyship_test"):
            if not connection.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
            ).fetchone():
                connection.execute(
                    sql.SQL("CREATE DATABASE {} OWNER anyship").format(sql.Identifier(name))
                )
    with psycopg.connect(
        host="127.0.0.1",
        port=PORT,
        user="anyship",
        password=values["password"],
        dbname="anyship",
        connect_timeout=5,
    ) as connection:
        connection.execute("SELECT 1")
    return database_url()


def start_if_managed(url):
    if CONFIG.exists() and make_url(url) == make_url(database_url()):
        start()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "stop", "status", "test"))
    args = parser.parse_args()
    if args.command in ("start", "test"):
        start()
        print(
            f"Local PostgreSQL ready: 127.0.0.1:{PORT} / anyship (credentials kept locally).",
            flush=True,
        )
        if args.command == "test":
            env = os.environ.copy()
            env["ANYSHIP_TEST_DATABASE_URL"] = database_url("anyship_test")
            return subprocess.call(
                [os.sys.executable, "-m", "pytest", "tests", "-q"], cwd=ROOT / "service", env=env
            )
    elif args.command == "stop":
        if is_running():
            run("pg_ctl", "-D", DATA, "-m", "fast", "-w", "-t", "30", "stop")
        print("Local PostgreSQL stopped; data preserved.")
    else:
        print("Local PostgreSQL is running." if is_running() else "Local PostgreSQL is stopped.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, psycopg.Error) as error:
        print(
            str(error)
            if isinstance(error, RuntimeError)
            else "PostgreSQL connection failed; check local configuration."
        )
        raise SystemExit(1) from None
