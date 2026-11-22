"""Every third-party image and download in qontinui-stack is pinned, and aged.

Plan 2026-11-22-fetches-stop-depending-on-upstreams, D5 and Phase 2. A floating
tag is a supply-chain door: whatever is pushed under it next is what the stack
runs. A pin to a digest closes it, but only if the digest was not itself
published a moment ago (the March 2026 Trivy compromise was a release trusted
the moment it appeared). So the rule has two halves, both checked here:

1. **Pinned.** Every compose ``image:`` and every Dockerfile ``FROM`` (and
   ``# syntax=`` frontend) names ``<name>:<tag>@sha256:<digest>``; every
   release archive a Dockerfile downloads is checked with ``sha256sum -c``
   against an ``ARG <NAME>_SHA256``.
2. **Aged.** Each pin carries ``# released: YYYY-MM-DD pinned: YYYY-MM-DD``
   (a trailing comment on a compose line; the line directly above a Dockerfile
   ``FROM`` or ``ARG``, because Dockerfile comments must start a line; the line
   directly BELOW ``# syntax=``, because a parser directive must be line one),
   and ``pinned - released >= COOLDOWN_DAYS``.

Exempt: locally built first-party images (``qontinui-*``; their own Dockerfiles
are checked instead), ``scratch``, and references to an earlier build stage.

Where ``released`` comes from, so it is read rather than typed: for an image,
the Docker Hub tag API's ``tag_last_pushed`` for the tag whose CURRENT digest
is the pinned one (the moment that digest became that tag's content), with the
digest cross-checked against the registry's ``Docker-Content-Digest``; for a
release archive, the upstream release API's ``published_at`` (the same source
``scripts/s3-candidates.pins.toml`` records).

COOLDOWN_DAYS is the plan's single number (D5). Its fleet home is Phase 3's
qontinui-claude-config ``scripts/fetch-pins-check.py``, which does not exist
yet; until it does, the one other copy in this repo
(``scripts/s3-candidates.pins.toml`` ``cooldown_days``) is asserted equal, so
the two cannot drift.

Stdlib only (runs in the ``python script unit tests`` job).
"""

from __future__ import annotations

import datetime as _dt
import re
import tomllib
import unittest
from pathlib import Path

COOLDOWN_DAYS = 14

REPO = Path(__file__).resolve().parents[2]

DIGEST = r"sha256:[0-9a-f]{64}"
PINNED_REF = re.compile(rf"^(?P<name>[a-z0-9][a-z0-9._/-]*):(?P<tag>[A-Za-z0-9._-]+)@{DIGEST}$")
AGE_COMMENT = re.compile(r"#\s*released:\s*(\d{4}-\d{2}-\d{2})\s+pinned:\s*(\d{4}-\d{2}-\d{2})\s*$")
FIRST_PARTY = re.compile(r"^qontinui-[a-z0-9._-]+(:[A-Za-z0-9._-]+)?$")


def _age_violation(comment_line: str, where: str) -> str | None:
    m = AGE_COMMENT.search(comment_line)
    if not m:
        return f"{where}: missing '# released: YYYY-MM-DD pinned: YYYY-MM-DD'"
    released, pinned = (_dt.date.fromisoformat(x) for x in m.groups())
    if released > pinned:
        return f"{where}: released {released} is after pinned {pinned}"
    days = (pinned - released).days
    if days < COOLDOWN_DAYS:
        return f"{where}: pinned {days} days after release, under the {COOLDOWN_DAYS}-day cooldown"
    return None


def check_compose(text: str, path: str) -> list[str]:
    """Violations for every ``image:`` line of a compose file."""
    out: list[str] = []
    for n, line in enumerate(text.splitlines(), 1):
        m = re.match(r"^\s*image:\s*(\S+)(.*)$", line)
        if not m:
            continue
        ref, rest = m.group(1).strip("\"'"), m.group(2)
        where = f"{path}:{n} {ref}"
        if FIRST_PARTY.match(ref):
            continue
        if not PINNED_REF.match(ref):
            out.append(f"{where}: not pinned as <name>:<tag>@sha256:<digest>")
            continue
        v = _age_violation(rest, where)
        if v:
            out.append(v)
    return out


def _logical_lines(text: str) -> list[tuple[int, str, str]]:
    """(first line number, joined instruction, the physical line above it)."""
    physical = text.splitlines()
    out: list[tuple[int, str, str]] = []
    i = 0
    while i < len(physical):
        start = i
        line = physical[i]
        joined = line
        while joined.rstrip().endswith("\\") and i + 1 < len(physical):
            i += 1
            joined = joined.rstrip()[:-1] + " " + physical[i]
        above = physical[start - 1] if start > 0 else ""
        out.append((start + 1, joined, above))
        i += 1
    return out


def check_dockerfile(text: str, path: str) -> list[str]:
    """Violations for the base images, frontend and downloads of a Dockerfile."""
    out: list[str] = []
    lines = text.splitlines()
    if lines and re.match(r"^#\s*syntax\s*=", lines[0]):
        ref = lines[0].split("=", 1)[1].strip()
        where = f"{path}:1 syntax={ref}"
        if not PINNED_REF.match(ref):
            out.append(f"{where}: frontend not pinned as <name>:<tag>@sha256:<digest>")
        else:
            v = _age_violation(lines[1] if len(lines) > 1 else "", where)
            if v:
                out.append(v)

    args: dict[str, str] = {}
    stages: set[str] = set()
    for n, line, above in _logical_lines(text):
        stripped = line.strip()
        arg = re.match(r"^ARG\s+([A-Za-z_][A-Za-z0-9_]*)=(\S+)", stripped)
        if arg:
            args[arg.group(1)] = arg.group(2).strip("\"'")
            if arg.group(1).endswith("_SHA256"):
                where = f"{path}:{n} ARG {arg.group(1)}"
                if not re.fullmatch(r"[0-9a-f]{64}", args[arg.group(1)]):
                    out.append(f"{where}: not a sha256 hex digest")
                v = _age_violation(above, where)
                if v:
                    out.append(v)
            continue
        frm = re.match(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", stripped, re.IGNORECASE)
        if frm:
            raw = frm.group(1)
            ref = re.sub(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", lambda m: args.get(m.group(1), m.group(0)), raw)
            if frm.group(2):
                stages.add(frm.group(2))
            where = f"{path}:{n} FROM {ref}"
            if ref == "scratch" or ref in stages - {frm.group(2)} or FIRST_PARTY.match(ref):
                continue
            if not PINNED_REF.match(ref):
                out.append(f"{where}: base image not pinned as <name>:<tag>@sha256:<digest>")
                continue
            v = _age_violation(above, where)
            if v:
                out.append(v)
            continue
        if re.match(r"^ADD\s", stripped, re.IGNORECASE) and re.search(r"https?://", stripped) \
                and "--checksum=sha256:" not in stripped:
            out.append(f"{path}:{n}: ADD of a URL without --checksum=sha256:")
        refs = re.findall(r"(?:--from=|\bfrom=)([^\s,]+)", stripped) if re.match(r"^(COPY|RUN)\s", stripped) else []
        for flag_ref in refs:
            # COPY --from= / RUN --mount=...,from= naming an IMAGE (not a stage).
            if flag_ref in stages or flag_ref.isdigit() or FIRST_PARTY.match(flag_ref):
                continue
            if not PINNED_REF.match(flag_ref):
                out.append(f"{path}:{n}: external image {flag_ref} not pinned as <name>:<tag>@sha256:<digest>")
        # A curl/wget INVOCATION (followed by a flag, quote, variable or URL),
        # not the package name in `apk add curl` / `apt-get install curl`.
        invocation = re.compile(r"\b(?:curl|wget)\s+(?:-|\"|'|\$|https?://)")
        if re.match(r"^RUN\s", stripped) and invocation.search(stripped):
            downloads = len(invocation.findall(stripped))
            checks = stripped.count("sha256sum -c")
            if checks < downloads:
                out.append(f"{path}:{n}: RUN has {downloads} curl/wget call(s) but {checks} 'sha256sum -c' check(s)")
    return out


def _repo_dockerfiles() -> list[Path]:
    return sorted(
        p for p in REPO.rglob("*Dockerfile*")
        if ".git" not in p.parts and not p.name.endswith(".dockerignore") and p.is_file()
    )


def _repo_compose_files() -> list[Path]:
    return sorted(
        p for pattern in ("docker-compose*.y*ml", "compose*.y*ml") for p in REPO.rglob(pattern)
        if ".git" not in p.parts and p.is_file()
    )


class RepoPinsTests(unittest.TestCase):
    def test_the_scan_sees_the_files_it_must(self) -> None:
        names = {str(p.relative_to(REPO)) for p in _repo_dockerfiles()}
        for required in ("sccache/Dockerfile", "migrator/Dockerfile", "web-prod/Dockerfile",
                         "local/postgres/tests/Dockerfile", "object-store/Dockerfile"):
            self.assertIn(required, names)
        self.assertIn("docker-compose.yml", {p.name for p in _repo_compose_files()})

    def test_every_compose_image_is_pinned_and_aged(self) -> None:
        violations: list[str] = []
        images = 0
        for p in _repo_compose_files():
            text = p.read_text()
            images += len(re.findall(r"^\s*image:", text, re.MULTILINE))
            violations += check_compose(text, str(p.relative_to(REPO)))
        self.assertGreater(images, 0, "no image: lines scanned")
        self.assertEqual(violations, [])

    def test_every_dockerfile_base_frontend_and_download_is_pinned_and_aged(self) -> None:
        violations: list[str] = []
        for p in _repo_dockerfiles():
            violations += check_dockerfile(p.read_text(), str(p.relative_to(REPO)))
        self.assertEqual(violations, [])

    def test_the_cooldown_has_one_value_in_this_repo(self) -> None:
        pins = tomllib.loads((REPO / "scripts" / "s3-candidates.pins.toml").read_text())
        self.assertEqual(pins["cooldown_days"], COOLDOWN_DAYS)


class RuleCanFailTests(unittest.TestCase):
    """Negative controls: each rule must fire on the defect it exists for
    [policy: testing/a-test-must-be-able-to-fail]."""

    D = "sha256:" + "a" * 64

    def test_floating_tag_fails(self) -> None:
        self.assertTrue(check_compose("    image: minio/minio:latest\n", "c"))

    def test_tag_without_digest_fails(self) -> None:
        self.assertTrue(check_compose("    image: redis:7.4.11-alpine # released: 2026-01-01 pinned: 2026-11-22\n", "c"))

    def test_digest_without_age_comment_fails(self) -> None:
        self.assertTrue(check_compose(f"    image: redis:7.4.11-alpine@{self.D}\n", "c"))

    def test_inside_the_cooldown_fails(self) -> None:
        v = check_compose(f"    image: redis:7@{self.D} # released: 2026-11-09 pinned: 2026-11-22\n", "c")
        self.assertEqual(len(v), 1)
        self.assertIn("13 days", v[0])

    def test_exactly_the_cooldown_passes(self) -> None:
        self.assertEqual(check_compose(f"    image: redis:7@{self.D} # released: 2026-11-08 pinned: 2026-11-22\n", "c"), [])

    def test_first_party_image_is_exempt(self) -> None:
        self.assertEqual(check_compose("    image: qontinui-canonical-coord:latest\n", "c"), [])

    def test_dockerfile_floating_from_fails(self) -> None:
        self.assertTrue(check_dockerfile("FROM alpine:3.20\n", "d"))

    def test_dockerfile_from_needs_the_comment_above(self) -> None:
        self.assertTrue(check_dockerfile(f"FROM alpine:3.23.7@{self.D}\n", "d"))
        ok = f"# released: 2026-10-23 pinned: 2026-11-22\nFROM alpine:3.23.7@{self.D}\n"
        self.assertEqual(check_dockerfile(ok, "d"), [])

    def test_dockerfile_from_resolves_arg_defaults(self) -> None:
        self.assertTrue(check_dockerfile("ARG BASE=python:3.12-slim\nFROM ${BASE}\n", "d"))
        self.assertEqual(check_dockerfile("ARG SLIM=qontinui-web-backend:slim-latest\nFROM ${SLIM}\n", "d"), [])

    def test_dockerfile_stage_reference_is_exempt(self) -> None:
        text = (f"# released: 2026-10-23 pinned: 2026-11-22\nFROM alpine:3.23.7@{self.D} AS base\n"
                "FROM base\n")
        self.assertEqual(check_dockerfile(text, "d"), [])

    def test_unpinned_syntax_frontend_fails(self) -> None:
        self.assertTrue(check_dockerfile("# syntax=docker/dockerfile:1.7\nFROM scratch\n", "d"))

    def test_unverified_download_fails(self) -> None:
        text = "FROM scratch\nRUN curl -fsSL https://example.invalid/tool.tgz \\\n    | tar -xz\n"
        self.assertTrue(check_dockerfile(text, "d"))

    def test_second_unverified_download_in_one_run_fails(self) -> None:
        text = ("FROM scratch\nRUN curl -fsSL -o /a https://x.invalid/a && echo \"h  /a\" | sha256sum -c - "
                "&& curl -fsSL -o /b \"$URL\"\n")
        self.assertTrue(check_dockerfile(text, "d"))

    def test_package_named_curl_is_not_a_download(self) -> None:
        self.assertEqual(check_dockerfile("FROM scratch\nRUN apt-get install -y python3 curl >/dev/null\n", "d"), [])

    def test_add_url_without_checksum_fails(self) -> None:
        self.assertTrue(check_dockerfile("FROM scratch\nADD https://x.invalid/t.tgz /t.tgz\n", "d"))

    def test_copy_from_external_image_fails_and_stage_passes(self) -> None:
        self.assertTrue(check_dockerfile("FROM scratch\nCOPY --from=busybox:latest /bin/sh /sh\n", "d"))
        text = (f"# released: 2026-10-23 pinned: 2026-11-22\nFROM alpine:3.23.7@{self.D} AS build\n"
                "FROM scratch\nCOPY --from=build /x /x\n")
        self.assertEqual(check_dockerfile(text, "d"), [])

    def test_sha_arg_needs_its_age(self) -> None:
        text = f"FROM scratch\nARG TOOL_SHA256={'b' * 64}\n"
        self.assertTrue(check_dockerfile(text, "d"))
        aged = f"FROM scratch\n# released: 2026-10-24 pinned: 2026-11-22\nARG TOOL_SHA256={'b' * 64}\n"
        self.assertEqual(check_dockerfile(aged, "d"), [])


if __name__ == "__main__":
    unittest.main()
