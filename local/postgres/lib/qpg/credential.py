"""D2 "Credentials": a password generated here, only its SCRAM verifier leaves.

``qontinui-pg credential new --role R --passfile F`` generates a 32-byte random
password on this host, writes it to a libpq passfile (mode 0600, owned by the
user running it — the ``postgres`` system user), and prints ONLY the
``SCRAM-SHA-256`` verifier. PostgreSQL accepts the verifier in
``CREATE ROLE … PASSWORD 'SCRAM-SHA-256$…'`` / ``ALTER ROLE … PASSWORD …``, so
the role can be created on RDS (through ECS Exec as qontinui_user) without the
password ever leaving this host. A verifier of a 32-byte random password
cannot be reversed in practice.

The verifier follows RFC 5802/7677 as PostgreSQL stores it:
``SCRAM-SHA-256$<iterations>:<salt>$<StoredKey>:<ServerKey>``. The password is
URL-safe base64 (ASCII), so SASLprep leaves it unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import stat


class CredentialError(RuntimeError):
    pass


def scram_verifier(password: str, salt: bytes | None = None, iterations: int = 4096) -> str:
    salt = salt if salt is not None else secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, 32)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def new_password() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def _escape(field: str) -> str:
    return field.replace("\\", "\\\\").replace(":", "\\:")


def _entry_user(line: str) -> str | None:
    parts, cur, esc = [], "", False
    for ch in line.rstrip("\n"):
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts[3] if len(parts) == 5 else None


def write_passfile_entry(passfile: str, role: str, password: str, host: str = "*", port: str = "*",
                         database: str = "*", rotate: bool = False) -> None:
    lines: list[str] = []
    if os.path.exists(passfile):
        st = os.stat(passfile)
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise CredentialError(f"{passfile} is readable beyond its owner (mode {oct(st.st_mode & 0o777)})")
        with open(passfile, encoding="utf-8") as fh:
            lines = fh.readlines()
    existing = [ln for ln in lines if _entry_user(ln) == role]
    if existing and not rotate:
        raise CredentialError(f"{passfile} already holds a password for {role}; pass --rotate to replace it")
    kept = [ln for ln in lines if _entry_user(ln) != role]
    entry = ":".join(_escape(f) for f in (host, port, database, role, password)) + "\n"
    tmp = passfile + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.writelines(kept + [entry])
    os.chmod(tmp, 0o600)
    os.replace(tmp, passfile)


def new_credential(role: str, passfile: str, host: str = "*", port: str = "*", database: str = "*",
                   rotate: bool = False) -> str:
    """Generate, store locally, return ONLY the verifier."""
    password = new_password()
    write_passfile_entry(passfile, role, password, host, port, database, rotate)
    return scram_verifier(password)
