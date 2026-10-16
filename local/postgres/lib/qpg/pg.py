"""A psql-driven Postgres client: stdlib only, no driver dependency.

Why psql and not psycopg: nothing in this repo depends on a Python Postgres
driver except the migrator image (psycopg2-binary, inside a container), and the
local database host installs ``postgresql-client-16`` with the server anyway.
Driving ``psql`` keeps every tool here runnable with the system ``python3``
alone, so the installer adds no pip step and no third-party package to a host
that holds production data.

Every query runs as ``psql -X --csv -t`` with ``ON_ERROR_STOP`` and a NULL
sentinel, so a NULL is never confused with an empty string (a NULL read is an
error in several callers, never a zero).

Values are passed into SQL through :func:`lit` / :func:`ident`, which quote with
standard_conforming_strings semantics (on by default since PostgreSQL 9.1) and
refuse NUL bytes.
"""

from __future__ import annotations

import csv
import io
import os
import subprocess
import sys
from dataclasses import dataclass, field

# A row can legitimately carry megabytes (the follower reads a jsonlog tail as
# one value); the csv module's default 128 KiB field limit would reject it.
csv.field_size_limit(sys.maxsize)

#: Printed by psql for SQL NULL. Contains a control character, so no value read
#: from a catalog or a test fixture can collide with it.
NULL = "\x01NULL\x01"

#: Settings every comparing session uses, so the text form of a row is the same
#: on both sides (timestamps, floats, intervals, bytea).
CANONICAL_SESSION = (
    "SET TimeZone = 'UTC'; SET DateStyle = 'ISO, YMD'; SET IntervalStyle = 'postgres'; "
    "SET extra_float_digits = 1; SET bytea_output = 'hex';"
)


class PgError(RuntimeError):
    """psql exited non-zero. ``stderr`` carries the server's message."""

    def __init__(self, label: str, returncode: int, stderr: str):
        super().__init__(f"{label}: psql exited {returncode}: {stderr.strip()}")
        self.label = label
        self.returncode = returncode
        self.stderr = stderr


def ident(name: str) -> str:
    """Quote an identifier."""
    if "\x00" in name:
        raise ValueError("NUL byte in identifier")
    return '"' + name.replace('"', '""') + '"'


def qualified(schema: str, name: str) -> str:
    return f"{ident(schema)}.{ident(name)}"


def lit(value: object) -> str:
    """Quote a literal (None becomes NULL)."""
    if value is None:
        return "NULL"
    text = str(value)
    if "\x00" in text:
        raise ValueError("NUL byte in literal")
    return "'" + text.replace("'", "''") + "'"


def text_array(values) -> str:
    """A text[] literal built from quoted elements."""
    return "ARRAY[" + ",".join(lit(v) for v in values) + "]::text[]"


@dataclass
class Psql:
    """One libpq conninfo plus the environment psql runs with.

    ``conninfo`` never carries a password: libpq reads it from the passfile
    named in ``env['PGPASSFILE']`` (mode 0600, owned by the service user).
    """

    conninfo: str
    label: str = "db"
    env: dict = field(default_factory=dict)
    psql: str = "psql"
    timeout: float = 600.0

    def _run(self, sql: str, extra: list[str] | None = None, stdin_tail: bytes = b"") -> str:
        cmd = [
            self.psql, "-X", "-q", "-v", "ON_ERROR_STOP=1", "--csv", "-t",
            "-P", f"null={NULL}", "-d", self.conninfo,
        ]
        if extra:
            cmd += extra
        cmd += ["-f", "-"]
        env = dict(os.environ)
        env.update(self.env)
        env.setdefault("PGCONNECT_TIMEOUT", "15")
        data = sql.encode("utf-8") + stdin_tail
        proc = subprocess.run(cmd, input=data, capture_output=True, env=env, timeout=self.timeout)
        if proc.returncode != 0:
            raise PgError(self.label, proc.returncode, proc.stderr.decode("utf-8", "replace"))
        return proc.stdout.decode("utf-8", "replace")

    def rows(self, sql: str, canonical: bool = False) -> list[list[str | None]]:
        """Run one query and return its rows; NULL becomes ``None``."""
        prefix = CANONICAL_SESSION + "\n" if canonical else ""
        out = self._run(prefix + sql.rstrip().rstrip(";") + ";\n")
        result = []
        for row in csv.reader(io.StringIO(out)):
            result.append([None if v == NULL else v for v in row])
        return result

    def scalar(self, sql: str) -> str | None:
        rows = self.rows(sql)
        if len(rows) != 1 or len(rows[0]) != 1:
            raise PgError(self.label, 0, f"expected one value, got {rows!r} for: {sql}")
        return rows[0][0]

    def execute(self, script: str, single_transaction: bool = True) -> str:
        """Run a script of statements; optionally as one transaction (``-1``)."""
        return self._run(script.rstrip() + "\n", ["-1"] if single_transaction else None)

    def copy_out(self, select_sql: str) -> bytes:
        """``COPY (select) TO STDOUT`` in text format, as raw bytes."""
        cmd = [self.psql, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", self.conninfo, "-f", "-"]
        env = dict(os.environ)
        env.update(self.env)
        script = CANONICAL_SESSION + f"\nCOPY ({select_sql}) TO STDOUT;\n"
        proc = subprocess.run(cmd, input=script.encode(), capture_output=True, env=env, timeout=self.timeout)
        if proc.returncode != 0:
            raise PgError(self.label, proc.returncode, proc.stderr.decode("utf-8", "replace"))
        return proc.stdout

    def execute_with_copy(self, before: str, copy_stmt: str, data: bytes, after: str = "") -> None:
        """One transaction: ``before``; ``COPY ... FROM STDIN`` fed ``data``; ``after``."""
        tail = data
        if tail and not tail.endswith(b"\n"):
            tail += b"\n"
        script = before.rstrip() + "\n" + copy_stmt.rstrip().rstrip(";") + ";\n"
        payload = tail + b"\\.\n" + after.encode("utf-8") + b"\n"
        self._run(script, ["-1"], stdin_tail=payload)
