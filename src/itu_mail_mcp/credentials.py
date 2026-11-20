"""Credential lookup: username from env, password from macOS Keychain (env as fallback)."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

KEYCHAIN_SERVICE = "itu-mail-mcp"


class CredentialError(RuntimeError):
    pass


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str
    source: str


def _keychain_password(username: str) -> str | None:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", username, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.rstrip("\n") or None


def load_credentials() -> Credentials:
    username = os.getenv("ITU_MAIL_USERNAME", "").strip()
    if not username:
        raise CredentialError("ITU_MAIL_USERNAME is not set.")

    password = _keychain_password(username)
    if password:
        return Credentials(username, password, "keychain")

    password = os.getenv("ITU_MAIL_PASSWORD")
    if password:
        return Credentials(username, password, "env")

    raise CredentialError(
        f"No password found. Store it with: security add-generic-password "
        f"-s {KEYCHAIN_SERVICE} -a {username} -w"
    )
