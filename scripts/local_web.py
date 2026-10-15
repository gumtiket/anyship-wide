"""Prepare local-only configuration without printing credentials; start a built UI."""

import argparse
import os
import socket
import subprocess
import sys
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import dotenv_values, set_key

from local_postgres import start as start_postgres
from local_postgres import start_if_managed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["github"], default="github")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    service = root / "service"
    config = service / ".env.github.local"
    if not config.exists():
        config.write_text(
            (service / ".env.web.example").read_text(encoding="utf-8"), encoding="utf-8"
        )
    values = dotenv_values(config)
    if not values.get("APP_AI_MODE"):
        set_key(config, "APP_AI_MODE", "placeholder")
        values = dotenv_values(config)
    if not values.get("APP_TOKEN_KEY"):
        set_key(config, "APP_TOKEN_KEY", Fernet.generate_key().decode())
        values = dotenv_values(config)
    if values.get("APP_DEMO", "").lower() != "false":
        print(f"Configuration mode mismatch. Check {config.name}.")
        return 1
    if values.get("APP_APP_ORIGIN") != "http://localhost:8000":
        print("This helper requires APP_APP_ORIGIN=http://localhost:8000.")
        return 1
    missing = [
        key
        for key in ("APP_GITHUB_CLIENT_ID", "APP_GITHUB_CLIENT_SECRET", "APP_GITHUB_APP_SLUG")
        if not values.get(key)
    ]
    if missing:
        print(f"Prepared: {config.name} (encryption key saved, not displayed).")
        print("Enter these values locally: " + ", ".join(missing))
        print("AnyShip will show the connection setup screen until these values are configured.")
    env = os.environ.copy()
    # A selected local profile is authoritative; no credentials appear in command arguments.
    env.update({key: value for key, value in values.items() if value is not None})
    if not (root / "frontend/dist/index.html").is_file():
        print("Frontend build missing. Run pnpm build in frontend first.")
        return 1
    print(f"Configuration ready: {args.mode}; frontend build found.")
    if args.check:
        if not values.get("APP_DATABASE_URL"):
            print("Database setup required. Install PostgreSQL binaries, then run without --check.")
            return 1
        return 0
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=1):
            print("Port 8000 is already in use. Existing local service: http://localhost:8000")
            print(
                "To change mode, stop the existing server first. This helper will not terminate other processes."
            )
            return 1
    except OSError:
        pass
    if not values.get("APP_DATABASE_URL"):
        url = start_postgres()
        set_key(config, "APP_DATABASE_URL", url)
        env["APP_DATABASE_URL"] = url
    else:
        start_if_managed(values["APP_DATABASE_URL"])
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=service, env=env, check=True
    )
    print("Open http://localhost:8000 | Stop: Ctrl+C", flush=True)
    try:
        return subprocess.call(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
                "--no-access-log",
            ],
            cwd=service,
            env=env,
        )
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
