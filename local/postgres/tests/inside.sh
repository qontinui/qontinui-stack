#!/usr/bin/env bash
# inside.sh — runs in the tools container (see run.sh). Green suite on /src,
# then each mutation in mutations.py against a mutated copy: its tests must FAIL.
set -uo pipefail
cd /src/tests || exit 2

MUTATE=1
ONLY=()
while [ $# -gt 0 ]; do
  case "$1" in
    --no-mutations) MUTATE=0 ;;
    --only) ONLY+=("$2"); shift ;;
  esac
  shift
done

TESTS=(test_installer test_follower)
[ ${#ONLY[@]} -gt 0 ] && TESTS=("${ONLY[@]}")

echo "==> GREEN: ${TESTS[*]} on the real tree"
if ! QPG_SRC=/src python3 -m unittest -v "${TESTS[@]}" 2>&1 | tee /tmp/green.log; then
  echo "RESULT: GREEN SUITE FAILED"
  exit 1
fi
grep -E "^(Ran|OK|FAILED)" /tmp/green.log

[ "$MUTATE" = 1 ] || { echo "RESULT: green (mutations skipped)"; exit 0; }

survived=0
while read -r name tests; do
  tree="/tmp/mut-$name"
  rm -rf "$tree"; cp -r /src "$tree"
  python3 /src/tests/mutations.py apply "$name" "$tree" || { echo "MUTATION $name: could not apply"; survived=1; continue; }
  for t in $tests; do
    if [ ${#ONLY[@]} -gt 0 ] && ! printf '%s\n' "${ONLY[@]}" | grep -qx "${t%%.*}"; then continue; fi
    if QPG_SRC="$tree" timeout 900 python3 -m unittest "$t" >"/tmp/mut-$name-$t.log" 2>&1; then
      echo "MUTATION $name -> $t: still GREEN (mutation survived)"
      tail -5 "/tmp/mut-$name-$t.log"
      survived=1
    else
      echo "MUTATION $name -> $t: RED as required ($(grep -E '^(FAILED|Ran)' "/tmp/mut-$name-$t.log" | tr '\n' ' '))"
      grep -E "AssertionError|Error:|halted" "/tmp/mut-$name-$t.log" | head -2 | sed 's/^/      /'
    fi
  done
done < <(python3 /src/tests/mutations.py list)

if [ "$survived" = 0 ]; then echo "RESULT: green, and every mutation turned its tests red"; exit 0; fi
echo "RESULT: A MUTATION SURVIVED"; exit 1
