"""Pins `branch_target_from_command` in `hooks/pre-checkout-coord-guard.sh`.

Plan `2026-09-27-command-segmenter-linear-time-and-shell-quote-model`, Phase 4.

The function used to run `tok="$(dequote ...)"` for every word after
`git checkout` / `git switch`, which forked a subshell per token. That cost
about 185 ms per token on a loaded Windows box, and `dequote`'s unconditional
`${t%\\"}`-style expansions were quadratic in the length of one long word. So a
checkout followed by a few hundred words, or by one very long word, ran past
the guard's 15 s PreToolUse timeout, and a timed-out hook fails OPEN. The
rewrite is fork-free and linear, and its output (the printed branch and the
return status) must not change on any input.

What each test pins:

* `test_matches_the_legacy_function`: the rewrite and a frozen copy of the old
  function give the same output and status, in the C and C.UTF-8 locales. The
  inputs are a table of every case arm, globs, quotes, invalid UTF-8 and long
  commands, plus a few hundred seeded random commands. The comparison runs in a
  directory whose files the table's globs expand to. Those include filenames
  with a newline or `\r\n` in them, which is the one kind of token that still
  takes the old `$(...)` path.
* `test_table_expectations`: the branch pinned for each table case with a known
  answer, so both copies cannot drift together unnoticed.
* `test_branch_target_is_linear`: a checkout followed by about 200 KB of words
  classifies inside a 5 s ceiling, and doubling the input costs less than 3x,
  taking the second-fastest of 5 interleaved runs. A quadratic walk gives about 4x. The
  per-token fork breaks the ceiling on its own. The output of each timed
  fixture is checked too.

Inputs reach bash through files, never argv or the environment.
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
_BASH = shutil.which("bash") or "bash"

# What `branch_target_from_command` needs from the guard, cut out by text
# because sourcing the whole guard would run it.
HOOK_FUNCTIONS = (
    "split_command_segments",
    "dequote_into",
    "dequote_print",
    "looks_like_branch",
    "branch_target_from_command",
)

# `dequote` and `branch_target_from_command` as they stood on origin/main
# aea20db, with comment lines dropped and the names suffixed `_legacy`. They
# are the oracle for every output the rewrite may produce: do NOT "fix" them.
# The copy calls the guard's current `split_command_segments` and
# `looks_like_branch`; both are unchanged by this rewrite, and the first is
# pinned byte-for-byte by `test_pre_checkout_split_segments.py`.
LEGACY_FUNCTIONS = r"""
dequote_legacy() {
  local t="$1"
  t="${t%\"}"; t="${t#\"}"
  t="${t%\'}"; t="${t#\'}"
  printf '%s' "$t"
}

branch_target_from_command_legacy() {
  local seg tok verb branch
  local create_branch first_positional positionals has_pathspec suppress
  local -a toks
  while IFS= read -r seg; do
    toks=( $seg )
    (( ${#toks[@]} )) || continue
    local i=0
    while (( i < ${#toks[@]} )) && [[ "${toks[$i]}" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; do
      i=$(( i + 1 ))
    done
    [[ "${toks[$i]:-}" == "git" ]] || continue
    i=$(( i + 1 ))
    while (( i < ${#toks[@]} )) && [[ "${toks[$i]}" == -* ]]; do
      case "${toks[$i]}" in
        -C|-c|--git-dir|--work-tree|--namespace|--exec-path) i=$(( i + 2 )) ;;
        *) i=$(( i + 1 )) ;;
      esac
    done
    verb="${toks[$i]:-}"
    [[ "$verb" == "checkout" || "$verb" == "switch" ]] || continue
    i=$(( i + 1 ))
    branch=""
    create_branch=""
    first_positional=""
    positionals=0
    has_pathspec=0
    suppress=0
    while (( i < ${#toks[@]} )); do
      tok="$(dequote_legacy "${toks[$i]}")"
      case "$tok" in
        --) has_pathspec=1; break ;;
        --pathspec-from-file=*|--pathspec-file-nul) has_pathspec=1; break ;;
        --detach|--orphan=*|--patch|-p) suppress=1; break ;;
        -b|-B|-c|-C|--orphan|--create|--force-create)
          create_branch="$(dequote_legacy "${toks[$(( i + 1 ))]:-}")"
          break
          ;;
        --pathspec-from-file)
          has_pathspec=1
          break
          ;;
        --conflict|--start-point|-t|--track)
          i=$(( i + 2 ))
          continue
          ;;
        -*) i=$(( i + 1 )); continue ;;
        [0-9]*'>'*|'>'*|'<'*)
          i=$(( i + 1 ))
          continue
          ;;
        *)
          positionals=$(( positionals + 1 ))
          if [[ -z "$first_positional" ]]; then
            first_positional="$tok"
          fi
          i=$(( i + 1 ))
          continue
          ;;
      esac
    done
    if [[ -n "$create_branch" ]]; then
      branch="$create_branch"
    elif (( suppress == 0 && has_pathspec == 0 && positionals == 1 )); then
      branch="$first_positional"
    else
      branch=""
    fi
    if looks_like_branch "$branch"; then
      printf '%s' "$branch"
      return 0
    fi
  done < <(split_command_segments "$1")
  return 1
}
"""

# Each input is followed by `<legacy output> NUL <legacy status> NUL <current
# output> NUL <current status> NUL`. The functions are called in a `||` list, as
# the guard calls them, so `set -e` is suspended inside them exactly as there.
DIFFERENTIAL_DRIVER = r"""
set -euo pipefail
mapfile -d '' -t inputs < "$1"
cd "$2"
# The files the table's globs expand to. They are made from bash rather than
# Python, because Git Bash can store a `\r`, a `\n` or a `"` in a filename (it
# maps them into private-use code points) where Windows Python cannot. A name
# the platform refuses is skipped: both copies see the same directory.
for name in main feat-glob-hit "'qfile'" '"dqfile"' $'nl-branch\n' \
    $'crf\r\n' $'\r\n' $'mid\nline' $'crmid\r\nx'; do
  { : > "$name"; } 2>/dev/null || true
done
for input in "${inputs[@]}"; do
  rc=0
  branch_target_from_command_legacy "$input" || rc=$?
  printf '\0%s\0' "$rc"
  rc=0
  branch_target_from_command "$input" || rc=$?
  printf '\0%s\0' "$rc"
done
"""

# One timed first run per fixture keeps its output and status. It aborts the
# moment a run blows its budget, so a regression fails in seconds rather than
# sitting through every round. Then come `rounds` interleaved timed runs.
# `EPOCHREALTIME` is `sec.usec`; dropping the radix gives microseconds.
PERF_DRIVER = r"""
set -euo pipefail
mapfile -d '' -t fixtures < "$1"
cd "$2"
rounds=$3
budget_us=$4
for i in "${!fixtures[@]}"; do
  rc=0
  t0=$EPOCHREALTIME
  branch_target_from_command "${fixtures[$i]}" > "out.$i" || rc=$?
  t1=$EPOCHREALTIME
  d=$(( ${t1//[.,]/} - ${t0//[.,]/} ))
  printf 'first %s %s %s\n' "$i" "$d" "$rc"
  if (( d > budget_us * (i + 1) )); then
    exit 0
  fi
done
for (( r = 0; r < rounds; r++ )); do
  for i in "${!fixtures[@]}"; do
    t0=$EPOCHREALTIME
    branch_target_from_command "${fixtures[$i]}" > /dev/null || true
    t1=$EPOCHREALTIME
    printf 'run %s %s\n' "$i" "$(( ${t1//[.,]/} - ${t0//[.,]/} ))"
  done
done
"""

LOCALES = ["C.UTF-8", "C"]


def _hook_function(name: str) -> str:
    """One function exactly as the guard defines it, cut from the script.

    The guard's style closes a top-level function with a lone `}` in column 0.
    """
    text = GUARD.read_text(encoding="utf-8")
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert m is not None, f"{name}() not found in {GUARD}"
    return m.group(0)


def _script(driver: str) -> bytes:
    body = "\n".join(_hook_function(n) for n in HOOK_FUNCTIONS)
    # A cut that stopped early would compile but test the wrong code.
    assert "done < <(split_command_segments \"$1\")" in body, body[-400:]
    return (body + LEGACY_FUNCTIONS + driver).encode()


def _run(tmp_path: Path, driver: str, inputs: list[bytes], locale: str,
         workdir: Path, *extra: str, timeout: int = 1800) -> bytes:
    for data in inputs:
        assert b"\0" not in data, "a bash string cannot hold NUL"
    script = tmp_path / "driver.sh"
    # Bytes, not write_text: on Windows text mode would write CRLF.
    script.write_bytes(_script(driver))
    feed = tmp_path / "inputs.bin"
    feed.write_bytes(b"".join(data + b"\0" for data in inputs))
    env = {k: v for k, v in os.environ.items() if k != "LANG" and not k.startswith("LC_")}
    env["LC_ALL"] = locale
    proc = subprocess.run(
        [_BASH, str(script), str(feed), str(workdir), *extra],
        env=env,
        capture_output=True,
        timeout=timeout,
    )
    stderr = proc.stderr.decode("utf-8", "replace")
    if f"cannot change locale ({locale})" in stderr:
        pytest.skip(f"locale {locale} is not installed here: {stderr.strip()}")
    assert proc.returncode == 0, stderr
    return proc.stdout


def _glob_dir(tmp_path: Path) -> Path:
    """An empty directory; DIFFERENTIAL_DRIVER fills it with the glob targets."""
    d = tmp_path / "globs"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Inputs

NONE = ""  # no branch: nothing printed, status 1
ANY = None  # not pinned: the differential alone decides

# (command, expected branch). The expectation is the old function's answer,
# written down so that the two copies cannot drift together. ANY marks an
# answer that depends on the platform or the locale, or is not worth spelling.
TABLE: list[tuple[bytes, str | None]] = [
    (b"git checkout -b feat/x", "feat/x"),
    (b"git switch -c feat/two", "feat/two"),
    (b"git checkout -B rel/1 origin/main", "rel/1"),
    (b"git checkout -C forced", "forced"),
    (b"git checkout --orphan newroot", "newroot"),
    (b"git switch --create c1", "c1"),
    (b"git switch --force-create c2", "c2"),
    (b"git checkout main", "main"),
    (b"git switch main", "main"),
    (b"cd /x && git switch -c feat/three", "feat/three"),
    (b"git -C /x checkout -b feat/four", "feat/four"),
    (b"git -c k=v --no-pager -P checkout feat/g", "feat/g"),
    (b"git --git-dir /g --work-tree /w checkout feat/wt", "feat/wt"),
    (b"git --namespace ns --exec-path /e switch feat/ns", "feat/ns"),
    (b"FOO=1 BAR_2=x=y git checkout feat/env", "feat/env"),
    (b"1X=bad git checkout m", NONE),
    (b"git checkout main >/dev/null 2>&1", "main"),
    (b"git checkout main 2>&1", "main"),
    (b"git switch feat/x >/dev/null 2>&1 <in", "feat/x"),
    (b"git checkout -- a", NONE),
    (b"git checkout .", NONE),
    (b"git checkout main -- a", NONE),
    (b"git checkout main src/foo.c", NONE),
    (b"git checkout --pathspec-from-file=list.txt main", NONE),
    (b"git checkout --pathspec-from-file list.txt main", NONE),
    (b"git checkout --pathspec-file-nul main", NONE),
    (b"git checkout 9763836e", NONE),
    (b"git switch --detach", NONE),
    (b"git switch --detach feat/d", NONE),
    (b"git checkout --orphan=o", NONE),
    (b"git checkout -p main", NONE),
    (b"git checkout --patch main", NONE),
    (b"git checkout -b", NONE),
    (b"git checkout -t origin/x", NONE),
    (b"git checkout --track origin/x feat/t", "feat/t"),
    (b"git checkout --conflict merge feat/c", "feat/c"),
    (b"git checkout --conflict=merge feat/c2", "feat/c2"),
    (b"git checkout --start-point x feat/s", "feat/s"),
    (b"git checkout -q -f --no-track feat/q", "feat/q"),
    (b"git checkout 'feat/sq'", "feat/sq"),
    (b'git checkout "feat/dq"', "feat/dq"),
    (b"git checkout -b 'feat/sq2'", "feat/sq2"),
    (b"git checkout \"'mix'\"", "mix"),
    (b"git checkout '\"'", '"'),
    (b"git checkout '\"'\"'\"'", ANY),
    (b"git checkout -b \"\"", NONE),
    (b"git checkout ''", NONE),
    (b"git checkout 'a b'", NONE),
    (b"git checkout HEAD", NONE),
    (b"git checkout ORIG_HEAD", NONE),
    (b"git checkout a~1", NONE),
    (b"git checkout x^", NONE),
    (b"git checkout a:b", NONE),
    (b"git checkout -", NONE),
    (b"git checkout ..", NONE),
    (b"git status && git checkout feat/second", "feat/second"),
    (b"git checkout HEAD; git switch feat/after", "feat/after"),
    (b"git checkout a|git switch feat/p", "a"),
    (b"git checkout -b feat/amp&", "feat/amp"),
    (b"echo git checkout nope", NONE),
    (b'git commit -m "git checkout -b nope"', NONE),
    (b"gitx checkout foo", NONE),
    (b"git", NONE),
    (b"", NONE),
    (b"   ", NONE),
    (b"git checkout", NONE),
    (b"git -C", NONE),
    (b"git -C /x", NONE),
    (b"git\tcheckout\tfeat/tab", "feat/tab"),
    (b"git checkout feat/cr\r", "feat/cr\r"),
    ("git checkout feat/ü".encode(), "feat/ü"),
    # Globs, expanded in the fixture directory. `ma?n` only resolves to the
    # file named `main`; `?qf*` resolves to a file named `'qfile'`, which the
    # create form then dequotes.
    (b"git checkout ma?n", "main"),
    (b"git checkout feat-g*", "feat-glob-hit"),
    (b"git checkout -b ?qf*", "qfile"),
    (b"git checkout [m]ain src", NONE),
    # Platform-dependent answers: pinned only by the differential. A filename
    # ending in a newline, or in `\r\n`, reaches the function only through a
    # glob. The old `$(dequote ...)` stripped trailing newlines, and on Git
    # Bash the `\r` before them as well. `??` matches only the name `\r\n`.
    (b"git checkout nl-bra*", ANY),
    (b"git checkout -b nl-bra*", ANY),
    (b"git checkout crf*", ANY),
    (b"git checkout -b crf*", ANY),
    (b"git checkout ??", ANY),
    (b"git checkout mid*", ANY),
    (b"git checkout crm*", ANY),
    (b"git checkout -b ?dq*", ANY),
    # Invalid UTF-8, including an encoded lone surrogate, which Git Bash's
    # pattern expansions re-encode in a UTF-8 locale when they remove a quote.
    (b"git checkout feat/\xff", ANY),
    (b"git checkout '\xed\xa0\x80'", ANY),
    (b"git checkout -b \"x\xed\xa0\x80\"", ANY),
    (b"git checkout \"\xf0\x9f\x9a\"", ANY),
]

PINNED = [(cmd, want) for cmd, want in TABLE if want is not ANY]


def _long_inputs() -> list[bytes]:
    return [
        # The checkout segment after a first segment that crosses the
        # segmenter's 512 / 8192 byte boundaries.
        b"echo " + b"x" * 8200 + b" && git checkout -b feat/after-long",
        b"echo '" + b"a;b" * 3000 + b"' ; git switch feat/after-quoted",
        # Many words after the verb, then the branch.
        b"git checkout " + b"-q " * 120 + b"feat/many",
        b"git checkout " + b"'-q' \"-f\" " * 60 + b"feat/many-quoted",
        b"git checkout feat/x " + b"src/file.c " * 100,
        # One long word.
        b"git checkout '" + b"abcdefgh" * 2000 + b"'",
    ]


# Random commands built from the words the function branches on.
_PREFIXES = ["", "", "", "FOO=1 ", "GIT_GUARD_CWD=/x ", "A=1 B=2 ", "1X=bad "]
_HEADS = ["git", "git", "git", "git", "echo", "gitx", "sudo git"]
_GLOBAL = ["", "", "-C /repo ", "-c k=v ", "--no-pager ", "-P ", "--git-dir /g ",
           "--work-tree=/w ", "--namespace ns ", "-C ", "--bare "]
_VERBS = ["checkout", "checkout", "switch", "switch", "commit", "status",
          "restore", "check-out", ""]
_ARGS = [
    "-b", "-B", "-c", "-C", "--orphan", "--orphan=o", "--create",
    "--force-create", "--detach", "-p", "--patch", "--", ".",
    "--pathspec-from-file", "--pathspec-from-file=f", "--pathspec-file-nul",
    "--conflict", "--conflict=merge", "--start-point", "-t", "--track", "-q",
    "-f", "--force", "-m", "--no-track", ">/dev/null", "2>&1", "2>", ">",
    "<in", "2>err", "main", "feat/x", "rel/1.2", "'feat/q'", '"feat/dq"',
    "'\"mix\"'", "\"'", "'", '"', "HEAD", "ORIG_HEAD", "9763836e",
    "abcdef1234567", "origin/main", "a~1", "x^", "a:b", "-", "..", "-x",
    "src/foo.c", "feat/ü", '""', "''", "'two words'", '"a b"',
]
_SEPARATORS = [" && ", " || ", "; ", " | ", "\n", " & ", ";"]


def _random_inputs() -> list[bytes]:
    rng = random.Random(20260927)
    out: list[bytes] = []
    for _ in range(300):
        segments = []
        for _ in range(rng.randint(1, 3)):
            args = " ".join(rng.choice(_ARGS) for _ in range(rng.randint(0, 6)))
            segments.append(
                rng.choice(_PREFIXES) + rng.choice(_HEADS) + " "
                + rng.choice(_GLOBAL) + rng.choice(_VERBS) + " " + args
            )
        command = segments[0]
        for seg in segments[1:]:
            command += rng.choice(_SEPARATORS) + seg
        out.append(command.encode())
    return out


def _split(raw: bytes, width: int) -> list[list[bytes]]:
    fields = raw.split(b"\0")
    assert fields[-1] == b"", "every field is NUL-terminated"
    fields = fields[:-1]
    assert len(fields) % width == 0, f"{len(fields)} fields"
    return [fields[i:i + width] for i in range(0, len(fields), width)]


# ---------------------------------------------------------------------------
# Tests


@pytest.mark.parametrize("locale", LOCALES)
def test_matches_the_legacy_function(tmp_path, locale):
    inputs = [cmd for cmd, _ in TABLE] + _long_inputs() + _random_inputs()
    raw = _run(tmp_path, DIFFERENTIAL_DRIVER, inputs, locale, _glob_dir(tmp_path))
    rows = _split(raw, 4)
    assert len(rows) == len(inputs)
    found = 0
    for data, (legacy, legacy_rc, current, current_rc) in zip(inputs, rows):
        assert (current, current_rc) == (legacy, legacy_rc), (
            f"{data[:160]!r}: legacy {legacy!r} rc={legacy_rc!r} "
            f"vs current {current!r} rc={current_rc!r}"
        )
        found += legacy_rc == b"0"
    # Enough of the inputs name a branch that the comparison is not only
    # agreeing on "nothing".
    assert found >= 60, found


@pytest.mark.parametrize("locale", LOCALES)
def test_table_expectations(tmp_path, locale):
    cmds = [cmd for cmd, _ in PINNED]
    raw = _run(tmp_path, DIFFERENTIAL_DRIVER, cmds, locale, _glob_dir(tmp_path))
    rows = _split(raw, 4)
    for (cmd, want), (legacy, legacy_rc, current, current_rc) in zip(PINNED, rows):
        expected = (b"", b"1") if want == NONE else (want.encode(), b"0")
        assert (current, current_rc) == expected, f"{cmd!r}: {current!r} rc={current_rc!r}"
        assert (legacy, legacy_rc) == expected, f"{cmd!r}: legacy {legacy!r}"


def _repeat(unit: str, n: int) -> str:
    return unit * max(1, n // len(unit))


# (name, builder, size of the smaller fixture, expected branch or None).
# The quoted fixture sends every token through the dequote path, so it uses a
# smaller size to keep the suite quick.
PERF_CASES = [
    ("flags", lambda n: "git checkout " + _repeat("-q --no-track 2>/dev/null ", n) + "feat/x",
     204_800, "feat/x"),
    ("positionals", lambda n: "git checkout feat/x " + _repeat("src/file.c ", n),
     204_800, None),
    ("quoted", lambda n: "git checkout " + _repeat("'-q' \"--no-track\" ", n) + "feat/q",
     102_400, "feat/q"),
    ("one-word", lambda n: "git checkout \"" + _repeat("abcdefgh", n) + "\"",
     204_800, "<the word>"),
]
CEILING_S = 5.0


def _robust_time(usecs: list[int]) -> float:
    """The second-fastest sample, in seconds.

    `EPOCHREALTIME` is wall time. Runner load only makes a sample slower, which
    a minimum ignores. A clock step can make one sample too FAST (on WSL one
    came out at -0.81 s), and a minimum would trust it. The second-fastest of 5
    absorbs one such step and up to three slow samples.
    """
    return sorted(usecs)[1] / 1e6


@pytest.mark.parametrize(
    "name,make,size,want", PERF_CASES, ids=[c[0] for c in PERF_CASES]
)
def test_branch_target_is_linear(tmp_path, name, make, size, want):
    rounds = 5
    fixtures = [make(size).encode(), make(2 * size).encode()]
    workdir = tmp_path / "work"
    workdir.mkdir()
    # The driver aborts after the first run that goes over the ceiling, but
    # only once that run returns. A regression to per-token forks can take
    # minutes, so the whole driver is bounded as well: every run allowed its
    # ceiling (twice that for the larger fixture), plus slack.
    limit = int(CEILING_S * 3 * (rounds + 1)) + 60
    try:
        raw = _run(tmp_path, PERF_DRIVER, fixtures, "C.UTF-8", workdir,
                   str(rounds), str(int(CEILING_S * 1e6)), timeout=limit)
    except subprocess.TimeoutExpired:
        pytest.fail(f"{name}: did not finish in {limit}s, far over the {CEILING_S}s ceiling")

    first: dict[int, tuple[int, str]] = {}
    runs: dict[int, list[int]] = {0: [], 1: []}
    for line in raw.decode().splitlines():
        parts = line.split()
        if parts[0] == "first":
            first[int(parts[1])] = (int(parts[2]), parts[3])
        else:
            runs[int(parts[1])].append(int(parts[2]))

    assert 0 in first, raw
    first_small = first[0][0] / 1e6
    assert first_small < CEILING_S, (
        f"{name}: {len(fixtures[0])} B took {first_small:.1f}s, over the "
        f"{CEILING_S}s ceiling"
    )
    assert 1 in first and all(len(v) == rounds for v in runs.values()), raw

    for i, data in enumerate(fixtures):
        got = (workdir / f"out.{i}").read_bytes()
        rc = first[i][1]
        if want is None:
            expected = (b"", "1")
        elif want == "<the word>":
            expected = (data[len(b'git checkout "'):-1], "0")
        else:
            expected = (want.encode(), "0")
        assert (got, rc) == expected, f"{name}: {got[:80]!r} rc={rc}"

    small = _robust_time(runs[0])
    large = _robust_time(runs[1])
    summary = (
        f"{name}: {len(fixtures[0])} B in {small:.3f}s, {len(fixtures[1])} B in "
        f"{large:.3f}s (2nd-fastest of {rounds}; all runs usec {runs})"
    )
    assert small < CEILING_S, f"over the {CEILING_S}s ceiling. {summary}"
    ratio = large / max(small, 0.001)
    assert ratio < 3.0, f"not linear, ratio {ratio:.2f} (quadratic is ~4). {summary}"
