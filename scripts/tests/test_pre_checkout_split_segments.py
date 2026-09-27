"""Pins `split_command_segments` in `hooks/pre-checkout-coord-guard.sh`.

Plan `2026-09-27-command-segmenter-linear-time-and-shell-quote-model`, Phase 4.

The function used to walk the command one character at a time with
`${s:i:1}`. In bash every substring expansion re-measures the WHOLE value, so
the walk was quadratic: 0.49 / 1.59 / 6.78 s at 10 / 20 / 40 KB on a Windows
Git Bash box. The guard runs synchronously on the Claude Code PreToolUse path
under a 15 s hook timeout, so a long checkout-class command timed the hook out,
and a timed-out hook fails OPEN. The rewrite is linear. It must not change a
single output byte, because `branch_target_from_command` reads its output line
by line.

What each test pins:

* `test_matches_the_legacy_walk`: the rewrite and a verbatim copy of the old
  character walk give byte-identical output over a table of constructs, the
  window and block boundaries, and a few hundred seeded random strings. It runs
  in the C and C.UTF-8 locales.
* `test_legacy_copy_matches_the_python_model`: the embedded legacy copy agrees
  with a small Python model of the same quote rules. That keeps the
  equivalence test from passing vacuously, for example if both functions printed
  nothing.
* `test_large_inputs_match_the_python_model`: the rewrite agrees with the model
  on inputs too long for the quadratic legacy walk to finish in a test. Those
  inputs cross the 64 KiB superblock boundary, the one boundary the legacy
  comparison cannot reach cheaply.
* `test_segmenting_is_linear`: a 200 KB command segments inside a generous
  budget, and 400 KB costs less than 3x 200 KB, taking the minimum of
  interleaved runs. A quadratic walk gives about 4x. The function's output on
  the same fixtures is checked against the model too, so a fast wrong answer
  cannot pass.
* `test_callers_locale_is_restored`: the rewrite runs under `local LC_ALL=C`,
  and the caller's locale must be back when it returns, whether it came from
  `LC_ALL` or from `LANG`.
* `test_walk_runs_in_the_c_locale` and its `_textually` twin: the walk really
  counts bytes. The first checks that `é` measures 2 inside the function and 1
  outside it. The second checks that the `local LC_ALL=C` line is present.

Invalid UTF-8 is in the table and the random alphabet on purpose. In a UTF-8
locale on Git Bash, a 4-byte character counts as two characters, and a
truncated sequence counts differently depending on the byte after it. Pattern
expansions there also rewrite some invalid bytes, for example an encoded lone
surrogate. A first draft that did arithmetic on character counts invented a
closing quote. Dropping `local LC_ALL=C` rewrites surrogate bytes. Ubuntu's
glibc does neither, so only a Windows run catches these inputs failing. The two
C-locale tests exist so that CI, on every platform, would notice if the
`local LC_ALL=C` line were removed or moved below the walk.

Everything is fed to bash through files, never argv or the environment, so the
sizes are not capped by the OS argument limits.
"""

from __future__ import annotations

import os
import random
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD = REPO_ROOT / "hooks" / "pre-checkout-coord-guard.sh"

# Resolve the interpreter rather than spawning bare `"bash"`: on Windows a bare
# `"bash"` resolves to the WSL launcher in System32 before $PATH is searched.
# Same idiom as the sibling suites (`test_pre_checkout_branch_events.py`).
_BASH = shutil.which("bash") or "bash"

# The function as it stood on origin/main db40446, copied verbatim apart from
# its name. It is the oracle for every output byte the rewrite may produce. Do
# NOT "fix" it: its quote model (no backslash escape, no `$(...)` nesting) is
# the contract the rewrite keeps.
LEGACY_FUNCTION = r"""
split_command_segments_legacy() {
  local s="$1" out="" q="" c i len
  len=${#s}
  for (( i = 0; i < len; i++ )); do
    c="${s:$i:1}"
    if [[ -n "$q" ]]; then
      out+="$c"
      [[ "$c" == "$q" ]] && q=""
      continue
    fi
    case "$c" in
      \'|\") q="$c"; out+="$c" ;;
      '&'|'|'|';'|$'\n') out+=$'\n' ;;
      *) out+="$c" ;;
    esac
  done
  printf '%s\n' "$out"
}
"""

# Both functions read every input from one NUL-separated file and write their
# outputs NUL-terminated, so a single bash process serves the whole table.
# `mapfile` reads a regular file buffered; `read -d ''` would be one syscall per
# byte. The driver runs under the guard's own `set -euo pipefail`, so an unbound
# variable or a failing command in the rewrite fails the test.
EQUIVALENCE_DRIVER = r"""
set -euo pipefail
mapfile -d '' -t inputs < "$1"
for input in "${inputs[@]}"; do
  split_command_segments_legacy "$input"
  printf '\0'
  split_command_segments "$input"
  printf '\0'
done
"""

CURRENT_ONLY_DRIVER = r"""
set -euo pipefail
mapfile -d '' -t inputs < "$1"
for input in "${inputs[@]}"; do
  split_command_segments "$input"
  printf '\0'
done
"""

# One untimed run per fixture keeps its output for the correctness check. Then
# `rounds` interleaved timed runs, so load that drifts during the test lands on
# every size rather than on one. `EPOCHREALTIME` is `sec.usec`; dropping the
# radix gives microseconds (a `,` radix is stripped too, whatever the locale).
PERF_DRIVER = r"""
set -euo pipefail
mapfile -d '' -t fixtures < "$1"
outdir=$2
rounds=$3
for i in "${!fixtures[@]}"; do
  split_command_segments "${fixtures[$i]}" > "$outdir/out.$i"
done
for (( r = 0; r < rounds; r++ )); do
  for i in "${!fixtures[@]}"; do
    t0=$EPOCHREALTIME
    split_command_segments "${fixtures[$i]}" > /dev/null
    t1=$EPOCHREALTIME
    printf '%s %s\n' "$i" "$(( ${t1//[.,]/} - ${t0//[.,]/} ))"
  done
done
"""

LOCALES = ["C.UTF-8", "C"]


def _current_function() -> str:
    """The function exactly as the guard defines it, cut from the script.

    Sourcing the whole guard would run it, so the function is cut out by text.
    The guard's style closes a top-level function with a lone `}` in column 0.
    """
    text = GUARD.read_text(encoding="utf-8")
    m = re.search(r"^split_command_segments\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert m is not None, f"split_command_segments() not found in {GUARD}"
    body = m.group(0)
    # A cut that stopped early would compile but test the wrong code.
    assert "printf '%s\\n' \"$out\"" in body, body
    return body


def _model(data: bytes) -> bytes:
    """The legacy quote rules, over bytes.

    Byte-wise is exact for this model. Every character it acts on is ASCII, and
    in UTF-8 an ASCII byte is never part of a multibyte character. Bash also
    reads an invalid byte as one character of its own.
    """
    out = bytearray()
    quote = None
    for byte in data:
        if quote is not None:
            out.append(byte)
            if byte == quote:
                quote = None
        elif byte in b"'\"":
            quote = byte
            out.append(byte)
        elif byte in b"&|;\n":
            out.append(0x0A)
        else:
            out.append(byte)
    return bytes(out) + b"\n"


def _run(tmp_path: Path, driver: str, inputs: list[bytes], locale: str,
         *extra: str, timeout: int = 600, locale_var: str = "LC_ALL") -> bytes:
    """Run `driver` with `locale` in `locale_var`, every other locale var unset."""
    for data in inputs:
        assert b"\0" not in data, "a bash string cannot hold NUL"
    script = tmp_path / "driver.sh"
    # Bytes, not write_text: on Windows text mode would write CRLF, and bash
    # reads the CR as part of each command.
    script.write_bytes((_current_function() + LEGACY_FUNCTION + driver).encode())
    feed = tmp_path / "inputs.bin"
    feed.write_bytes(b"".join(data + b"\0" for data in inputs))
    env = {k: v for k, v in os.environ.items() if k != "LANG" and not k.startswith("LC_")}
    env[locale_var] = locale
    proc = subprocess.run(
        [_BASH, str(script), str(feed), *extra],
        env=env,
        capture_output=True,
        timeout=timeout,
    )
    stderr = proc.stderr.decode("utf-8", "replace")
    # Skip only on the warning that names the locale this test asked for. Any
    # other "cannot change locale" would come from the function itself, and
    # that is a failure.
    if f"cannot change locale ({locale})" in stderr:
        pytest.skip(f"locale {locale} is not installed here: {stderr.strip()}")
    assert proc.returncode == 0, stderr
    return proc.stdout


# ---------------------------------------------------------------------------
# Inputs

# Every construct the old walk handled, including the ones its model gets
# "wrong" on purpose (a backslash is not an escape; `$(...)` is not nesting).
TABLE: list[bytes] = [
    b"",
    b"git checkout main",
    b"git checkout -b feat/x && git push -u origin feat/x",
    b"git fetch || git switch main",
    b"a;b;c",
    b"a & b",
    b"a | b |& c",
    b"&&||;;|&",
    b"git checkout main >/dev/null 2>&1",
    b"git switch feat/x 2>&1 | tee log",
    b"a\nb\n",
    b"trailing;",
    b"trailing\n",
    b"trailing &&",
    b"trailing |",
    b";;;",
    b"\n\n",
    b'git commit -m "git checkout -b nope; x" && git switch z',
    b"echo 'a;b' ; git switch y",
    b"echo 'it''s' ; x",
    b"echo \"a'b;c\" ; d",
    b"echo 'a\"b|c' | d",
    b"''",
    b'""',
    b"'",
    b'"',
    b"echo 'unterminated; git switch x",
    b'echo "unterminated && git switch x',
    # A backslash is NOT an escape in this model, so `\"` opens a quote.
    b'echo \\"; git switch x',
    b"echo it\\'s; git switch y",
    b"echo \\\\; y",
    b"$(git checkout -b x; y) && `z | w`",
    b"x=\"$(printf 'a;b')\"; git switch y",
    b"a\r\nb;c\r\n",
    b"a\tb;\tc",
    "echo 'héllo; wörld' ; git switch ünïcödé".encode(),
    "日本語;テスト|x&y".encode(),
    "🚀'🚀;'🚀;x\"é|\"".encode(),
    # Invalid UTF-8: a stray lead byte before a quote, lone continuation
    # bytes, and a truncated sequence at the very end.
    b"x\xc3'a;b'\xff;\xfe|y\xe6\x97",
    b"\x80\x81;'\xc3';\xf0\x9f\x9a",
    # Truncated 4-byte sequences beside quotes. On Git Bash these count
    # differently depending on the next byte, and a first draft of the rewrite
    # that did arithmetic on character counts invented a closing quote here.
    b"\xf0\x9f\x9a'\xf0\x9a\x80",
    b"\xf0\x9f\x9a'\xf0\x9a\x80;x' ; y",
    b"a\"\xf0\x9f\x9a\"|\xf0\x9f;'\xf0'&z",
    # An encoded lone surrogate. On Git Bash a pattern expansion run in a
    # UTF-8 locale rewrites it to other bytes, which is why the rewrite runs
    # its walk under `local LC_ALL=C`.
    b'a\xed\xa0\x80"',
    b"x" * 510 + b'\xed\xa0\x80"' + b";y",
]


def _boundary_inputs() -> list[bytes]:
    """A quote, a separator or a split character at the 512 / 8192 boundaries.

    The rewrite slices the command into 512-byte windows inside 8 KiB blocks,
    so each boundary is where a carried-over quote state could go wrong, and
    where a multibyte character is cut in two between windows.
    """
    out: list[bytes] = []
    for pad in (510, 511, 512, 513, 1023, 1024, 8191, 8192, 8193):
        out.append((" " * pad + "'x;y'" + ";z").encode())
        out.append(("x" * pad + ";" + "'q'").encode())
    # Two-byte characters, so the quote lands at bytes 510 / 512 / 514 and
    # 8190 / 8192 / 8194. The one-byte lead shifts every `é` onto an odd
    # offset, so one of them straddles each boundary.
    for n in (255, 256, 257, 4095, 4096, 4097):
        out.append(("é" * n + '"a|b"' + "&c").encode())
        out.append(("x" + "é" * n + "'a;b'" + "|c").encode())
    # A 4-byte character straddling the first window boundary, and a
    # truncated one ending exactly on it before a quote.
    out.append(("x" * 510 + "🚀" + "'a;b'" + ";c").encode())
    out.append(b"x" * 509 + b"\xf0\x9f\x9a" + b"'a;b'" + b";c")
    # A quote that opens in one window and closes several windows later.
    out.append(("'" + "a;" * 400 + "'" + ";b").encode())
    # A quote that spans the 8 KiB block boundary, then a separator after it.
    out.append(("x" * 8000 + '"' + "a;b" * 100 + '"' + ";z").encode())
    # A quote left open across a block boundary and never closed.
    out.append(("y" * 8100 + "'" + "c|d" * 50).encode())
    return out


def _random_inputs() -> list[bytes]:
    """Seeded random strings. The quote characters are dense, so the quote
    state flips often and runs cross windows in both states."""
    rng = random.Random(20260927)
    ascii_alphabet = [c.encode() for c in ["a", "b", "'", '"', "&", "|", ";", " ", "\n", "x"]]
    wide_alphabet = ascii_alphabet + [c.encode() for c in ["é", "日", "🚀"]]
    # Truncated sequences, a lone lead byte, a lone continuation byte, and a
    # byte that is never valid UTF-8.
    # An encoded lone surrogate too.
    invalid_alphabet = wide_alphabet + [
        b"\xf0\x9f\x9a", b"\xe6\x97", b"\xc3", b"\x80", b"\xff", b"\xed\xa0\x80",
    ]
    out: list[bytes] = []
    for alphabet, count in ((ascii_alphabet, 300), (wide_alphabet, 60), (invalid_alphabet, 60)):
        for _ in range(count):
            n = rng.randrange(0, 1300)
            out.append(b"".join(rng.choice(alphabet) for _ in range(n)))
    return out


def _repeat_to(unit: str, n: int) -> str:
    return (unit * (n // len(unit) + 1))[:n]


# A realistic mix: quotes, separators, redirections and words.
MIXED_UNIT = (
    'git checkout -b feat && git commit -m "fix: it\'s a; b && c | d" && '
    "echo 'x;y' ; printf \"%s\\n\" a b | cat & ls -la 2>&1 >/dev/null; "
)
PROSE_UNIT = "the quick brown fox jumps over the lazy dog; it's a | b & c "
QUOTE_DENSE_UNIT = "'a'\"b\" "


def _sparse(n: int) -> str:
    """A checkout, then one long double-quoted body: the common long shape."""
    head = "git checkout -b feat/long && git commit -m \""
    return head + _repeat_to(PROSE_UNIT, n - len(head) - 1) + '"'


# ---------------------------------------------------------------------------
# Tests


@pytest.mark.parametrize("locale", LOCALES)
def test_matches_the_legacy_walk(tmp_path, locale):
    inputs = TABLE + _boundary_inputs() + _random_inputs()
    raw = _run(tmp_path, EQUIVALENCE_DRIVER, inputs, locale)
    outputs = raw.split(b"\0")
    assert outputs[-1] == b"", "every output is NUL-terminated"
    outputs = outputs[:-1]
    assert len(outputs) == 2 * len(inputs), (
        f"expected {2 * len(inputs)} outputs, got {len(outputs)}"
    )
    for i, data in enumerate(inputs):
        legacy, current = outputs[2 * i], outputs[2 * i + 1]
        assert current == legacy, (
            f"input #{i} ({len(data)} bytes) {data[:120]!r}...: "
            f"legacy {legacy[:200]!r} vs current {current[:200]!r}"
        )


@pytest.mark.parametrize("locale", LOCALES)
def test_legacy_copy_matches_the_python_model(tmp_path, locale):
    inputs = TABLE + _random_inputs()[:100]
    raw = _run(tmp_path, EQUIVALENCE_DRIVER, inputs, locale)
    outputs = raw.split(b"\0")[:-1]
    assert len(outputs) == 2 * len(inputs)
    for i, data in enumerate(inputs):
        assert outputs[2 * i] == _model(data), f"input #{i}: {data[:120]!r}"


@pytest.mark.parametrize("locale", LOCALES)
def test_large_inputs_match_the_python_model(tmp_path, locale):
    rng = random.Random(7)
    alphabet = ["a", "b", "'", '"', "&", "|", ";", " ", "\n", "x", "é", "日", "🚀"]
    inputs = [
        _repeat_to(MIXED_UNIT, 70_000).encode(),
        _sparse(140_000).encode(),
        # A quote that opens before the 64 KiB superblock boundary and closes
        # after it, then a separator the guard must still see.
        ("z" * 65_000 + "'" + "a;b" * 400 + "'" + ";git switch y").encode(),
        # Multibyte throughout: 70,000 characters are about 200 KB of UTF-8.
        _repeat_to("日本'語;テ'スト|é\"ü;\"🚀&", 70_000).encode(),
        "".join(rng.choice(alphabet) for _ in range(140_000)).encode(),
    ]
    raw = _run(tmp_path, CURRENT_ONLY_DRIVER, inputs, locale)
    outputs = raw.split(b"\0")[:-1]
    assert len(outputs) == len(inputs)
    for i, data in enumerate(inputs):
        assert outputs[i] == _model(data), f"input #{i} ({len(data)} bytes)"


# (fixture name, the two sizes, the budget for the smaller size in seconds).
# The quote-dense fixture is the worst case: the cost is a constant per quote
# character, so it uses smaller sizes to keep the suite quick. A quadratic
# walk still gives about 4x across a doubling at those sizes.
PERF_CASES = [
    ("sparse", _sparse, 204_800, 5.0),
    ("mixed", lambda n: _repeat_to(MIXED_UNIT, n), 204_800, 5.0),
    ("quote-dense", lambda n: _repeat_to(QUOTE_DENSE_UNIT, n), 51_200, 5.0),
]


@pytest.mark.parametrize(
    "name,make,size,budget", PERF_CASES, ids=[c[0] for c in PERF_CASES]
)
def test_segmenting_is_linear(tmp_path, name, make, size, budget):
    # Five interleaved rounds. The sparse fixture takes about 13 ms on Linux,
    # so one burst of runner load must not decide the ratio.
    rounds = 5
    fixtures = [make(size).encode(), make(2 * size).encode()]
    outdir = tmp_path / "out"
    outdir.mkdir()
    raw = _run(tmp_path, PERF_DRIVER, fixtures, "C.UTF-8", str(outdir), str(rounds))

    for i, data in enumerate(fixtures):
        got = (outdir / f"out.{i}").read_bytes()
        assert got == _model(data), f"{name}: wrong output at {len(data)} bytes"

    timings: dict[int, list[int]] = {0: [], 1: []}
    for line in raw.decode().splitlines():
        idx, usec = line.split()
        timings[int(idx)].append(int(usec))
    assert all(len(v) == rounds for v in timings.values()), timings

    small = min(timings[0]) / 1e6
    large = min(timings[1]) / 1e6
    summary = (
        f"{name}: {size} B in {small:.3f}s, {2 * size} B in {large:.3f}s "
        f"(min of {rounds}; all runs usec {timings})"
    )
    assert small < budget, f"over the {budget}s budget. {summary}"
    # max() keeps a sub-millisecond small run from inflating the ratio.
    ratio = large / max(small, 0.001)
    assert ratio < 3.0, f"not linear, ratio {ratio:.2f} (quadratic is ~4). {summary}"


LOCALE_DRIVER = r"""
set -euo pipefail
x=$'\xc3\xa9'
before=${#x}
split_command_segments "a;b 'c|d'" > /dev/null
after=${#x}
printf '%s %s %s\n' "$before" "$after" "${LC_ALL-<unset>}"
"""


@pytest.mark.parametrize("locale_var", ["LC_ALL", "LANG"])
def test_callers_locale_is_restored(tmp_path, locale_var):
    """`local LC_ALL=C` must not leak: `é` is one character before and after.

    The `LANG` case is the usual production shape. There `LC_ALL` starts out
    unset, and returning from the function has to leave it UNSET, not merely
    empty.
    """
    raw = _run(tmp_path, LOCALE_DRIVER, [], "C.UTF-8", locale_var=locale_var)
    if not raw.startswith(b"1 "):
        # Measured BEFORE the call, so a skip here cannot hide a regression.
        pytest.skip(f"C.UTF-8 is not in effect via {locale_var} here: {raw!r}")
    expected_lc_all = b"C.UTF-8" if locale_var == "LC_ALL" else b"<unset>"
    assert raw == b"1 1 " + expected_lc_all + b"\n", raw


# `split_command_segments` ends with `printf`, so a shell function that
# shadows the builtin runs INSIDE its dynamic scope and measures the locale in
# effect when the function prints. The `_textually` test pins that the locale
# is set before the walk too. fd 3 carries the reading past the `> /dev/null`.
C_LOCALE_DRIVER = r"""
set -euo pipefail
exec 3>&1
p=$'\xc3\xa9'
builtin printf 'outer=%s\n' "${#p}"
printf() { local q=$'\xc3\xa9'; builtin printf 'inner=%s\n' "${#q}" >&3; builtin printf "$@"; }
split_command_segments "a;b" > /dev/null
"""


def test_walk_runs_in_the_c_locale_textually():
    """Pin the `local LC_ALL=C` line AND its position: it must be the function's
    first statement.

    The behavioural test below measures the locale only when the function
    prints, so a line moved down to just before that `printf` would pass it
    while the walk itself ran in the caller's locale. Under glibc the output
    would still be right, so only this check would catch that on Linux CI.
    This check also still runs where C.UTF-8 is missing and the behavioural
    tests skip.
    """
    body = _current_function()
    code = [
        line for line in body.splitlines()[1:]
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert code[0] == "  local LC_ALL=C", code[:3]


@pytest.mark.parametrize("locale_var", ["LC_ALL", "LANG"])
def test_walk_runs_in_the_c_locale(tmp_path, locale_var):
    """The walk really counts bytes: `é` is 2 inside it and 1 outside it."""
    raw = _run(tmp_path, C_LOCALE_DRIVER, [], "C.UTF-8", locale_var=locale_var)
    if not raw.startswith(b"outer=1\n"):
        pytest.skip(f"C.UTF-8 is not in effect via {locale_var} here: {raw!r}")
    assert raw == b"outer=1\ninner=2\n", raw
