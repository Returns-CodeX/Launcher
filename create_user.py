#!/usr/bin/env python3
"""Create/update a local CodeX account using a salted PBKDF2 verifier."""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
from pathlib import Path

ITERATIONS = 600_000
DEFAULT_PATH = Path(__file__).resolve().parent / "Security" / "users.json"


def main():
    target = Path(input(f"Accounts file [{DEFAULT_PATH}]: ").strip() or DEFAULT_PATH).expanduser()
    username = input("Username (3-80 characters): ").strip()
    if not 3 <= len(username) <= 80 or not re.fullmatch(r"[A-Za-z0-9_. -]+", username):
        raise SystemExit("Use 3-80 letters, numbers, spaces, dots, underscores, or hyphens.")
    display_name = input(f"Display name [{username}]: ").strip() or username
    password = getpass.getpass("Password (use a unique, long password): ")
    confirmation = getpass.getpass("Confirm password: ")
    if len(password) < 12:
        raise SystemExit("Use at least 12 characters.")
    if password != confirmation:
        raise SystemExit("Passwords did not match.")
    try:
        data = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {"version": 1, "users": []}
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Could not read existing accounts file: {exc}")
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("users"), list):
        raise SystemExit("Existing account file has an unsupported format.")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS, dklen=32)
    record = {"username": username, "display_name": display_name[:80], "salt": salt.hex(), "iterations": ITERATIONS, "password_hash": digest.hex()}
    users = [u for u in data["users"] if not (isinstance(u, dict) and str(u.get("username", "")).casefold() == username.casefold())]
    users.append(record)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"version": 1, "users": users}, indent=2) + "\n", encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    print(f"Account verifier saved for {username!r} in {target}. The password itself was not saved.")


if __name__ == "__main__":
    main()
