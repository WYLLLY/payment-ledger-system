"""Create a local, ignored environment file with independent random passwords."""

import argparse
import os
import secrets
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--add-worker-secret", action="store_true")
    args = parser.parse_args()
    destination = Path(".env")
    if args.add_worker_secret:
        if any(
            line.startswith("WORKER_DB_PASSWORD=") for line in destination.read_text().splitlines()
        ):
            print("Worker secret already configured; unchanged.")
            return
        with destination.open("a") as file:
            file.write(f"\nWORKER_DB_PASSWORD={secrets.token_urlsafe(32)}\n")
        print("Added worker secret; existing settings preserved.")
        return
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as file:
        for name in (
            "POSTGRES_PASSWORD",
            "MIGRATION_DB_PASSWORD",
            "APP_DB_PASSWORD",
            "WORKER_DB_PASSWORD",
        ):
            file.write(f"{name}={secrets.token_urlsafe(32)}\n")
        file.write(
            "APP_DB_HOST=127.0.0.1\nAPP_DB_PORT=15432\nAPP_DB_NAME=payment_ledger\nAPI_PORT=8000\n"
        )
    print("Created .env with fresh local secrets; existing files are never overwritten.")


if __name__ == "__main__":
    main()
