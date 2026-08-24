#!/usr/bin/env python3
"""Fail CI when a value that terraform must redact is not marked ``sensitive``.

Why this exists
---------------
``docs/terraform-state-secret-inventory.md`` "What follows from it" 1 records
that terraform redacts a plan diff only for attributes it *knows* are sensitive,
so the redaction "is only ever as good as somebody having marked the attribute".
PR #70 fixed the one instance that had gone unmarked — ``variable "alert_email"``
in ``aws/modules/cost-control``, the operator's personal address rendering
verbatim in every plan diff of a **public** repo — and the note it left says so
in as many words:

    this is a class that recurs the next time a value is added without the
    marker, not a defect that was retired.

Nothing in CI caught the first one. ``terraform validate`` does not check
sensitivity, ``terraform fmt`` is whitespace, and the drift classifier's unit
tests never look at ``aws/``. So the recurrence had no guard at all. This is
that guard: a **source-only, credential-free, state-free** check — exactly the
class ``.github/workflows/qontinui-ci.yml`` is allowed to run on a public repo.
It reads ``.tf`` files and nothing else: no backend, no provider, no plan, no
state object.

The three rules
---------------
Each rule is a *flow* invariant with a mechanical antecedent, not a guess at
whether a name looks secret. A name heuristic would fire on ``domain_name`` and
miss ``signup_allowlist``; these fire on where a value provably came from.

``R1  insecure-value-needs-sensitive-sink``
    A ``module`` argument whose expression reads ``.insecure_value`` must land in
    a child ``variable`` declared ``sensitive = true``.

    ``aws_ssm_parameter``'s ``insecure_value`` is the accessor that *deliberately
    drops* terraform's sensitivity marking — ``aws/staging/main.tf`` uses it
    twice, on purpose, because ``.value`` is unconditionally sensitive and would
    demand a KMS grant and produce a perpetual cosmetic diff. Dropping the marker
    there is only safe because it is re-applied at the module boundary. R1 is
    that debt made checkable: exactly the fix PR #70 applied to ``alert_email``
    by hand, and the pattern ``signup_allowlist`` already followed.

``R2  sensitive-input-needs-sensitive-source``
    If a ``module`` argument targets a child variable marked ``sensitive`` and the
    expression is a bare ``var.<name>`` of the *calling* module, that calling
    variable must be marked too.

    Sensitivity propagates forward from a value's origin; it does not travel
    backwards from the sink. An unmarked caller variable renders unredacted
    anywhere *else* the caller uses it, and the child's marker cannot reach that.
    ``first_superuser_email`` is marked in both places today, and that is the
    shape this rule pins.

``R3  sensitive-var-needs-sensitive-output``
    An ``output`` whose ``value`` references a ``sensitive`` variable of the same
    module must itself be ``sensitive``.

    Terraform raises "Output refers to sensitive values" only for a **root**
    module output; a child module's is accepted silently, because sensitivity
    propagates dynamically at apply time. That measured asymmetry — recorded in
    PR #70's own commit message — is why the ``output "alert_email"`` half of
    that fix could not have been caught by ``terraform validate``, and why it
    needs a lint rather than a plan.

Exit codes: ``0`` clean, ``1`` findings, ``2`` bad usage.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterator, NamedTuple

# ---------------------------------------------------------------------------
# HCL masking
# ---------------------------------------------------------------------------
#
# Every structural scan below runs over a MASK: a string the same length as the
# source in which each character that is not HCL *code* has been replaced by a
# space (newlines kept, so offsets and line numbers still line up). String
# literals keep their `"` delimiters, so the label of `variable "alert_email"` is
# recoverable by slicing the ORIGINAL at the offsets the mask found.
#
# Masking is not fussiness. `aws/staging/main.tf` contains
#
#     backend_cors_origin_regex = "^https://([a-z0-9-]+\\.)*${replace(var.domain_name, ".", "\\.")}$"
#
# — quotes nested two deep inside a string — and a naive `"`-toggling scanner
# desynchronises there and mis-slices every block after it in the file.
# Interpolation (`${...}`) is masked back to CODE on purpose: the `var.`
# references inside `"https://${var.coord_subdomain}.${var.domain_name}"` are
# real references, and R3 has to see them.

_HEREDOC = re.compile(r"<<-?([A-Za-z_][A-Za-z0-9_]*)")


def mask_source(src: str) -> str:
    """Blank comments, literal string text and heredoc bodies; keep code."""
    out = list(src)
    i, n = 0, len(src)

    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    # Open contexts: "str" (inside a quoted literal) or "interp" (inside a
    # ${...} inside one). Empty means plain code.
    stack: list[str] = []

    while i < n:
        c = src[i]

        if stack and stack[-1] == "str":
            if c == "\\":
                blank(i, i + 2)
                i += 2
                continue
            if c == '"':
                stack.pop()  # the closing delimiter itself stays visible
                i += 1
                continue
            if src.startswith("$${", i) or src.startswith("%%{", i):
                # HCL's escapes for a LITERAL `${` / `%{`. Blanking all three
                # characters is what stops the scanner opening an interpolation
                # that never closes and reading the rest of the file as code.
                blank(i, i + 3)
                i += 3
                continue
            if src.startswith("${", i) or src.startswith("%{", i):
                stack.append("interp")
                i += 2  # the marker is code, and so is everything up to its `}`
                continue
            blank(i, i + 1)
            i += 1
            continue

        # --- code: top level, or inside an interpolation ---
        if c == "#" or src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
            continue
        if c == '"':
            stack.append("str")
            i += 1
            continue
        if stack and stack[-1] == "interp":
            # Brace depth INSIDE an interpolation, so a `{...}` in there — an
            # object literal, a `for` expression — closes itself rather than
            # closing the interpolation and dropping the scanner back into
            # string mode one level too early.
            if c == "{":
                stack.append("interp")
                i += 1
                continue
            if c == "}":
                stack.pop()
                i += 1
                continue
        m = _HEREDOC.match(src, i)
        if m:
            tag = m.group(1)
            j = src.find("\n", m.end())
            if j < 0:
                blank(i, n)
                i = n
                continue
            end = n
            terminator = re.compile(r"^[ \t]*%s[ \t]*$" % re.escape(tag), re.M)
            hit = terminator.search(src, j)
            if hit:
                end = hit.end()
            blank(i, end)
            i = end
            continue
        i += 1

    return "".join(out)


# ---------------------------------------------------------------------------
# Block / attribute extraction
# ---------------------------------------------------------------------------

_HEADER = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)((?:[ \t]+\"[^\"\n]*\")*)[ \t]*\{", re.M)
_LABEL = re.compile(r"\"([^\"\n]*)\"")
# An attribute STARTS a statement, which is not the same as starting a LINE.
# `aws/modules/cost-control/main.tf` writes
#
#     output "budget_name" { value = aws_budgets_budget.monthly.name }
#
# on one line — a form `terraform fmt` preserves — so anchoring on `^` alone
# reads that block as having no attributes at all. Silent, and in the direction
# that reports a missing marker as clean.
_ATTR = re.compile(
    r"(?:^|(?<=\{)|(?<=\})|(?<=\n))[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*", re.M
)
_OPEN, _CLOSE = "([{", ")]}"


class Block(NamedTuple):
    type: str
    labels: tuple[str, ...]
    start: int  # offset of the character after the opening `{`
    end: int  # offset of the matching `}`


def _match_brace(mask: str, open_at: int) -> int:
    """Offset of the `}` closing the `{` at ``open_at``; ``len(mask)`` if unbalanced."""
    depth = 0
    for j in range(open_at, len(mask)):
        if mask[j] == "{":
            depth += 1
        elif mask[j] == "}":
            depth -= 1
            if depth == 0:
                return j
    return len(mask)


def top_level_blocks(src: str, mask: str) -> Iterator[Block]:
    """Column-0 blocks: `variable "x" {`, `module "y" {`, `output "z" {`, ..."""
    for m in _HEADER.finditer(mask):
        open_at = m.end() - 1
        end = _match_brace(mask, open_at)
        labels = tuple(_LABEL.findall(src[m.start(2) : m.end(2)]))
        yield Block(m.group(1), labels, open_at + 1, end)


def _depth_between(mask: str, a: int, b: int) -> int:
    depth = 0
    for ch in mask[a:b]:
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth -= 1
    return depth


def _expression_end(mask: str, start: int, limit: int) -> int:
    """End of an attribute's expression.

    Whichever comes first at bracket depth 0: a newline, or the closing brace of
    the enclosing block (the single-line ``{ value = x }`` form).
    """
    depth = 0
    for j in range(start, limit):
        ch = mask[j]
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            if depth == 0:
                return j
            depth -= 1
        elif ch == "\n" and depth <= 0:
            return j
    return limit


def body_attributes(src: str, mask: str, block: Block) -> Iterator[tuple[str, str, int]]:
    """``(name, expression, offset)`` for attributes at the block's OWN level.

    Depth-checked, so a ``validation { condition = ... }`` nested inside a
    ``variable`` block cannot be mistaken for the variable's own attribute — the
    difference between reading a real ``sensitive = true`` and reading one that
    belongs to something else.
    """
    for m in _ATTR.finditer(mask, block.start, block.end):
        if _depth_between(mask, block.start, m.start()) != 0:
            continue
        yield (
            m.group(1),
            src[m.end() : _expression_end(mask, m.end(), block.end)],
            m.end(),
        )


def line_of(src: str, offset: int) -> int:
    return src.count("\n", 0, offset) + 1


# ---------------------------------------------------------------------------
# Module model
# ---------------------------------------------------------------------------

_TRUE = re.compile(r"^\s*true\s*$")
_BARE_VAR = re.compile(r"^\s*var\.([A-Za-z_][A-Za-z0-9_]*)\s*$")
_VAR_REF = re.compile(r"\bvar\.([A-Za-z_][A-Za-z0-9_]*)")


class Finding(NamedTuple):
    path: Path
    line: int
    rule: str
    message: str

    def render(self, relative_to: Path | None = None) -> str:
        p = self.path
        if relative_to is not None:
            try:
                p = self.path.relative_to(relative_to)
            except ValueError:
                pass
        return f"{p.as_posix()}:{self.line}: [{self.rule}] {self.message}"


class TerraformModule:
    """One directory of ``.tf`` files, parsed once."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.variables: dict[str, bool] = {}  # name -> declared sensitive
        self._files: list[tuple[Path, str, str]] = []
        for path in sorted(directory.glob("*.tf")):
            src = path.read_text(encoding="utf-8")
            mask = mask_source(src)
            self._files.append((path, src, mask))
            for block in top_level_blocks(src, mask):
                if block.type == "variable" and block.labels:
                    self.variables[block.labels[0]] = _declares_sensitive(src, mask, block)

    def blocks(self, kind: str) -> Iterator[tuple[Path, str, str, Block]]:
        for path, src, mask in self._files:
            for block in top_level_blocks(src, mask):
                if block.type == kind:
                    yield path, src, mask, block


def _declares_sensitive(src: str, mask: str, block: Block) -> bool:
    return any(
        name == "sensitive" and _TRUE.match(expr)
        for name, expr, _ in body_attributes(src, mask, block)
    )


def _resolve_source(caller_dir: Path, expr: str) -> Path | None:
    """Local module source -> directory. Registry / git sources return ``None``."""
    m = re.search(r"\"([^\"\n]+)\"", expr)
    if not m:
        return None
    source = m.group(1)
    if not source.startswith((".", "/")):
        return None  # registry or git — not ours to read
    return (caller_dir / source).resolve()


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def source_files(root: Path) -> list[Path]:
    """This repo's ``.tf`` files — never terraform's own vendored copies.

    The CI step runs AFTER ``terraform init`` in the same job, so
    ``aws/staging/.terraform/`` exists by then. It holds no ``.tf`` today
    (every module source here is a local path), but the moment one is a registry
    or git source, init vendors that module's files under
    ``.terraform/modules/`` — and linting third-party code we cannot fix would
    turn this guard into noise somebody switches off.
    """
    return sorted(
        p
        for p in root.rglob("*.tf")
        if not any(part.startswith(".") for part in p.relative_to(root).parts)
    )


def check_tree(root: Path) -> list[Finding]:
    """Run every rule over the terraform tree rooted at ``root``."""
    directories = sorted({p.parent for p in source_files(root)})
    modules = {d.resolve(): TerraformModule(d) for d in directories}
    findings: list[Finding] = []
    for module in modules.values():
        findings += _check_module_calls(module, modules)
        findings += _check_outputs(module)
    return sorted(findings, key=lambda f: (f.path.as_posix(), f.line, f.rule))


def _check_module_calls(
    caller: TerraformModule, modules: dict[Path, TerraformModule]
) -> list[Finding]:
    findings: list[Finding] = []
    for path, src, mask, block in caller.blocks("module"):
        label = block.labels[0] if block.labels else "?"
        attrs = list(body_attributes(src, mask, block))
        source_expr = next((e for n, e, _ in attrs if n == "source"), None)
        target = _resolve_source(path.parent, source_expr) if source_expr else None
        child = modules.get(target) if target else None

        for name, expr, offset in attrs:
            if name == "source":
                continue
            line = line_of(src, offset)
            child_sensitive = child.variables.get(name) if child else None

            if ".insecure_value" in expr:
                if child_sensitive is None:
                    # An unresolvable sink is reported, not passed over. A rule
                    # that cannot see where the value lands has checked nothing,
                    # and silence there would read as a clean result.
                    why = "no readable local source" if child is None else "variable not declared"
                    findings.append(
                        Finding(
                            path,
                            line,
                            "insecure-value-unresolved-sink",
                            f'module "{label}" passes an `.insecure_value` to `{name}`, but that '
                            f'module\'s `variable "{name}"` could not be read ({why}) — the '
                            f"redaction owed at this boundary is unverifiable.",
                        )
                    )
                elif not child_sensitive:
                    findings.append(
                        Finding(
                            path,
                            line,
                            "insecure-value-needs-sensitive-sink",
                            f'module "{label}" passes an `.insecure_value` to `{name}` — that '
                            f"accessor deliberately drops terraform's sensitivity marking — but "
                            f'`variable "{name}"` in {_display(child.dir)} is not `sensitive = true`, '
                            f"so the value renders verbatim in every plan diff that touches it.",
                        )
                    )

            if child_sensitive:
                bare = _BARE_VAR.match(expr)
                if bare and caller.variables.get(bare.group(1)) is False:
                    findings.append(
                        Finding(
                            path,
                            line,
                            "sensitive-input-needs-sensitive-source",
                            f'module "{label}" feeds `var.{bare.group(1)}` into `{name}`, which is '
                            f'`sensitive = true` — but `variable "{bare.group(1)}"` here is not, so '
                            f"the value still renders unredacted anywhere else this module uses it.",
                        )
                    )
    return findings


def _check_outputs(module: TerraformModule) -> list[Finding]:
    findings: list[Finding] = []
    sensitive = {n for n, s in module.variables.items() if s}
    if not sensitive:
        return findings
    for path, src, mask, block in module.blocks("output"):
        label = block.labels[0] if block.labels else "?"
        attrs = list(body_attributes(src, mask, block))
        value = next((e for n, e, _ in attrs if n == "value"), None)
        if value is None:
            continue
        marked = any(n == "sensitive" and _TRUE.match(e) for n, e, _ in attrs)
        referenced = sorted(set(_VAR_REF.findall(value)) & sensitive)
        if referenced and not marked:
            carried = ", ".join("var." + r for r in referenced)
            findings.append(
                Finding(
                    path,
                    line_of(src, block.start),
                    "sensitive-var-needs-sensitive-output",
                    f'output "{label}" carries {carried}, declared `sensitive = true` in this '
                    f"module, but the output itself is not marked. Terraform only errors on this "
                    f"for a ROOT output, so nothing else catches it here.",
                )
            )
    return findings


def _display(path: Path) -> str:
    try:
        return path.relative_to(Path.cwd()).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "aws",
        help="terraform tree to check (default: aws/)",
    )
    args = ap.parse_args(argv)
    root: Path = args.root
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    findings = check_tree(root)
    base = Path.cwd()
    for finding in findings:
        print(finding.render(base))
    if findings:
        print(
            f"\n{len(findings)} sensitivity finding(s). Each one renders a value verbatim in "
            f"`terraform plan` output, and this repo is PUBLIC — see "
            f"docs/terraform-state-secret-inventory.md.",
            file=sys.stderr,
        )
        return 1
    # Report the file count, so "clean" cannot be confused with "found nothing
    # to read" — a checker that silently parsed zero files also prints clean.
    print(f"terraform sensitivity lint: clean ({len(source_files(root))} .tf files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
