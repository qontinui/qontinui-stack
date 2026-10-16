"""The one configuration file, ``/etc/qontinui-pg/qontinui-pg.conf`` (INI).

The committed ``qontinui-pg.conf.example`` carries every key with a placeholder
or a default. Host-specific values (the tailnet address, the publisher's host
name, repository locations) live only in the installed copy on the host: this
repository is public.
"""

from __future__ import annotations

import configparser
import os
import shlex
from dataclasses import dataclass

from .pg import Psql

DEFAULT_PATH = "/etc/qontinui-pg/qontinui-pg.conf"
PLACEHOLDER = "REPLACE_ME"


class ConfigError(RuntimeError):
    pass


def load(path: str | None = None) -> "Config":
    path = path or os.environ.get("QONTINUI_PG_CONFIG") or DEFAULT_PATH
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # keep env var names' case in [follower.env]
    if not parser.read(path, encoding="utf-8"):
        raise ConfigError(f"config not readable: {path}")
    return Config(parser, path)


def _split_list(value: str) -> list[str]:
    return [v.strip() for v in value.replace("\n", ",").split(",") if v.strip()]


@dataclass
class HistoryTable:
    table: str  # schema-qualified
    lane: str

    @property
    def schema(self) -> str:
        return self.table.split(".", 1)[0]

    @property
    def name(self) -> str:
        return self.table.split(".", 1)[1]


class Config:
    def __init__(self, parser: configparser.ConfigParser, path: str):
        self.p = parser
        self.path = path

    # -- raw access ---------------------------------------------------------
    def get(self, section: str, key: str, fallback: str | None = None) -> str | None:
        return self.p.get(section, key, fallback=fallback)

    def getint(self, section: str, key: str, fallback: int) -> int:
        return self.p.getint(section, key, fallback=fallback)

    def getbool(self, section: str, key: str, fallback: bool) -> bool:
        return self.p.getboolean(section, key, fallback=fallback)

    def require(self, section: str, key: str) -> str:
        value = self.p.get(section, key, fallback=None)
        if value is None or not value.strip():
            raise ConfigError(f"[{section}] {key} is required ({self.path})")
        if PLACEHOLDER in value:
            raise ConfigError(f"[{section}] {key} still holds the {PLACEHOLDER} placeholder ({self.path})")
        return value.strip()

    # -- connections ----------------------------------------------------------
    def _psql(self, section: str, key: str, label: str, appname: str) -> Psql:
        env = {"PGAPPNAME": appname}
        passfile = self.get(section, "passfile")
        if passfile:
            env["PGPASSFILE"] = passfile
        return Psql(conninfo=self.require(section, key), label=label, env=env,
                    psql=self.get("cluster", "psql", fallback="psql") or "psql")

    def publisher(self, appname: str = "qontinui-pg") -> Psql:
        """The replication role's read-only connection to the publisher."""
        return self._psql("publisher", "dsn", "publisher", appname)

    def sub_admin(self, appname: str = "qontinui-pg") -> Psql:
        """The local superuser (subscription owner) on the subscriber."""
        return self._psql("subscriber", "admin_dsn", "subscriber-admin", appname)

    def sub_app(self, appname: str = "qontinui-pg") -> Psql:
        """The application role (``qontinui_user``) on the subscriber."""
        return self._psql("subscriber", "app_dsn", "subscriber-app", appname)

    def reverse(self, section: str, appname: str = "qontinui-pg") -> Psql | None:
        if not (self.get(section, "reverse_dsn") or "").strip():
            return None
        env = {"PGAPPNAME": appname}
        passfile = self.get(section, "reverse_passfile") or self.get("publisher", "passfile")
        if passfile:
            env["PGPASSFILE"] = passfile
        return Psql(conninfo=self.require(section, "reverse_dsn"), label="reverse", env=env)

    # -- replication scope ------------------------------------------------------
    @property
    def subscription(self) -> str:
        return self.require("replication", "subscription")

    @property
    def publication(self) -> str:
        return self.require("replication", "publication")

    @property
    def published_schemas(self) -> list[str]:
        return _split_list(self.require("replication", "published_schemas"))

    @property
    def app_role(self) -> str:
        return self.get("cluster", "app_role", fallback="qontinui_user") or "qontinui_user"

    @property
    def follower_role(self) -> str:
        return self.get("cluster", "follower_role", fallback="qontinui_follower") or "qontinui_follower"

    # -- follower ------------------------------------------------------------------
    @property
    def version_table(self) -> str:
        return self.get("follower", "version_table", fallback="public.alembic_version") or "public.alembic_version"

    @property
    def coord_lane(self) -> bool:
        return self.getbool("follower", "coord_lane", False)

    @property
    def phase8_complete(self) -> bool:
        return self.getbool("follower", "phase8_complete", False)

    @property
    def coord_history_tables(self) -> list[HistoryTable]:
        raw = self.get("follower", "coord_history_tables", fallback="") or ""
        tables = []
        for item in _split_list(raw):
            if ":" not in item or "." not in item.split(":", 1)[0]:
                raise ConfigError(f"[follower] coord_history_tables entry {item!r} is not schema.table:lane")
            table, lane = item.split(":", 1)
            tables.append(HistoryTable(table.strip(), lane.strip()))
        return tables

    @property
    def coord_version_column(self) -> str:
        return self.get("follower", "coord_version_column", fallback="version") or "version"

    def command(self, key: str, **values: str) -> list[str]:
        """A ``[follower]`` command template, split and filled in."""
        template = self.require("follower", key)
        return [part.format(**values) for part in shlex.split(template)]

    def optional_command(self, key: str, **values: str) -> list[str] | None:
        template = (self.get("follower", key) or "").strip()
        if not template:
            return None
        return [part.format(**values) for part in shlex.split(template)]

    def command_env(self) -> dict:
        env = {}
        if self.p.has_section("follower.env"):
            for key, value in self.p.items("follower.env"):
                env[key] = value
        passfile = self.get("subscriber", "passfile")
        if passfile:
            env.setdefault("PGPASSFILE", passfile)
        return env

    def a4_violations(self) -> list[str]:
        """A4: after Phase 8 the follower carries no coord source at all."""
        if not self.phase8_complete:
            return []
        found = []
        if self.coord_lane:
            found.append("[follower] coord_lane = true")
        for key in ("coord_migrate_cmd", "coord_adopt_cmd", "coord_history_tables"):
            if (self.get("follower", key) or "").strip():
                found.append(f"[follower] {key} is set")
        return found
