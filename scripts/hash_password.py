#!/usr/bin/env python3
# ABOUTME: CLI helper to generate a bcrypt hash for ARCHIVER_ADMIN_PASSWORD_HASH
# ABOUTME: Usage: python scripts/hash_password.py  (reads password from stdin)
"""Generate a bcrypt hash for the admin password."""

from __future__ import annotations

import getpass
import sys

MIN_PASSWORD_LEN = 8


def main() -> int:
    try:
        from archiver.auth import hash_password
    except ImportError:
        print("error: run via 'uv run python scripts/hash_password.py'", file=sys.stderr)
        return 1

    if sys.stdin.isatty():
        pw = getpass.getpass("Admin password: ")
        confirm = getpass.getpass("Confirm: ")
        if pw != confirm:
            print("error: passwords do not match", file=sys.stderr)
            return 1
    else:
        pw = sys.stdin.read().strip()

    if len(pw) < MIN_PASSWORD_LEN:
        print("error: password must be at least 8 characters", file=sys.stderr)
        return 1

    print(hash_password(pw))
    return 0


if __name__ == "__main__":
    sys.exit(main())
