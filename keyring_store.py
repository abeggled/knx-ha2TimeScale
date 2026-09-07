"""KNX IP Secure keyring: load, validate and describe an ETS keyring export.

The file and its password live in the service state directory so that both
the collector and the UI (same system user) can read them, and so the UI can
replace them without root.
"""
from __future__ import annotations

import os
import pathlib

STATE_DIR = pathlib.Path(
    os.environ.get("STATE_DIRECTORY", "/var/lib/knx-ha2timescale").split(":")[0]
)
KEYRING_FILE = STATE_DIR / "keyring.knxkeys"
PASSWORD_FILE = STATE_DIR / "keyring.pass"


def keyring_password() -> str | None:
    """Environment wins; otherwise the file the UI wrote."""
    env_name = os.environ.get("KNX_KEYRING_PASSWORD_ENV", "KNX_KEYRING_PASSWORD")
    if pw := os.environ.get(env_name):
        return pw
    try:
        return PASSWORD_FILE.read_text().strip() or None
    except OSError:
        return None


def store(data: bytes, password: str) -> list[dict]:
    """Validate, then persist. Returns the tunnel interfaces found.

    Nothing is written unless the keyring actually opens with the password —
    a wrong password must fail here, not at the next service start.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_DIR / "keyring.tmp"
    tmp.write_bytes(data)
    try:
        interfaces = describe(tmp, password)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(KEYRING_FILE)
    KEYRING_FILE.chmod(0o600)
    PASSWORD_FILE.write_text(password)
    PASSWORD_FILE.chmod(0o600)
    return interfaces


def describe(path, password: str) -> list[dict]:
    """Tunnel interfaces in the keyring — host, user id, individual address."""
    from xknx.secure.keyring import sync_load_keyring

    keyring = sync_load_keyring(str(path), password)
    out = []
    for iface in keyring.interfaces:
        out.append({
            "host": str(iface.host) if iface.host else None,
            "user_id": iface.user_id,
            "individual_address": str(
                getattr(iface, "individual_address", "") or ""),
            "type": str(getattr(iface, "type", "") or ""),
        })
    return sorted(out, key=lambda i: (i["host"] or "", i["user_id"] or 0))


def status() -> dict:
    """What the UI shows without needing the password again."""
    if not KEYRING_FILE.exists():
        return {"present": False}
    stat = KEYRING_FILE.stat()
    return {
        "present": True,
        "path": str(KEYRING_FILE),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "password_stored": PASSWORD_FILE.exists(),
    }
