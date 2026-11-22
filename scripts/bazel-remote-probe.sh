#!/usr/bin/env bash
# bazel-remote-probe.sh — host-side health signal for the stack's bazel-remote.
#
# The upstream image (buchgr/bazel-remote-cache) is distroless: no shell, no
# wget, so docker-compose.yml cannot give the container a healthcheck. This is
# the health read instead, through the published HTTP port:
#
#   GET http://127.0.0.1:${BAZEL_REMOTE_HTTP_PORT:-9094}/status
#
# Exit codes:
#   0  HEALTHY  — 200 and a JSON body carrying bazel-remote's status fields
#   1  UNHEALTHY — it answered, but not with a 200 + status JSON
#   2  UNKNOWN  — no answer at all (not listening, refused, timed out). That
#                 is not evidence the service is down; read `docker compose ps`
#                 and `docker compose logs bazel-remote` next.
#
# Options: --url <base> (default http://127.0.0.1:${BAZEL_REMOTE_HTTP_PORT:-9094}),
#          --timeout <secs> (default 10).
set -uo pipefail

URL="http://127.0.0.1:${BAZEL_REMOTE_HTTP_PORT:-9094}"
TIMEOUT=10
while [ $# -gt 0 ]; do
    case "$1" in
        --url) URL="$2"; shift 2 ;;
        --timeout) TIMEOUT="$2"; shift 2 ;;
        -h|--help) sed -n '2,/^set -uo/p' "$0" | sed 's/^# \{0,1\}//;$d'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 64 ;;
    esac
done

body="$(mktemp)"
trap 'rm -f "$body"' EXIT
code="$(curl -s -o "$body" -w '%{http_code}' --max-time "$TIMEOUT" "$URL/status")"
rc=$?
if [ "$rc" -ne 0 ] || [ "$code" = "000" ]; then
    echo "bazel-remote: UNKNOWN — no answer from $URL/status (curl exit $rc)"
    exit 2
fi
if [ "$code" = "200" ] && python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
sys.exit(0 if isinstance(d, dict) and ("CurrSize" in d or "MaxSize" in d or "NumFiles" in d) else 1)
' "$body" 2>/dev/null; then
    echo "bazel-remote: HEALTHY — $URL/status answered 200 with status JSON"
    exit 0
fi
echo "bazel-remote: UNHEALTHY — $URL/status answered $code: $(head -c 200 "$body")"
exit 1
