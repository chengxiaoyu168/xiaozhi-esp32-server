#!/usr/bin/env python3
"""Write a local OTA path credential. Never prints the value."""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import secrets

DEST = (
    Path(__file__).resolve().parents[2]
    / "main"
    / "xiaozhi-server"
    / "data"
    / ".ota-bootstrap"
)


def main() -> int:
    rotate = "--rotate" in sys.argv[1:]
    DEST.parent.mkdir(parents=True, exist_ok=True)
    if DEST.exists() and not rotate:
        print(
            "data/.ota-bootstrap already exists. Pass --rotate to revoke the old URL.",
            file=sys.stderr,
        )
        return 1
    value = secrets.token_urlsafe(32)
    fd = os.open(DEST, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    os.chmod(DEST, stat.S_IRUSR | stat.S_IWUSR)
    print(
        "Wrote data/.ota-bootstrap mode 600. "
        "Read it only on this machine when setting the firmware OTA URL. "
        "Do not print, paste, log, or commit it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
