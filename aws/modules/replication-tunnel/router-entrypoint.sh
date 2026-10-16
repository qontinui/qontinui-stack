#!/bin/sh
# Entrypoint of the replication router's tailscale container
# (aws/modules/replication-tunnel). Rendered into the task definition as the
# argument of `/bin/sh -c`, so it runs from the pinned tailscale/tailscale image
# with no image of our own to build.
#
# Inputs (task-definition environment / secrets):
#   TS_STATE_ARN       arn:aws:ssm:...:parameter/... holding tailscaled's state.
#                      tailscaled creates the parameter on first boot (as a
#                      SecureString) and rewrites it on every state change, so a
#                      restarted task keeps its node identity instead of spending
#                      a fresh auth key.
#   TS_AUTHKEY         injected from Secrets Manager by the execution role. Used
#                      only by the first login; with state already present,
#                      `tailscale up` keeps the existing node and ignores it.
#   TS_HOSTNAME        the node's tailnet hostname.
#   TS_ROUTES          comma-separated CIDRs to advertise (the DB subnet group).
#   TS_SOCKS5_ADDR     loopback address of tailscaled's SOCKS5 outbound proxy,
#                      which the forwarder container dials.
#   TS_UP_TIMEOUT      how long `tailscale up` may take before the task fails.
#
# Any failure exits non-zero; the container is essential, so ECS stops the task
# and the service starts a new one. Nothing here loops or retries by itself.
set -eu

: "${TS_STATE_ARN:?}" "${TS_AUTHKEY:?}" "${TS_HOSTNAME:?}" "${TS_ROUTES:?}"
: "${TS_SOCKS5_ADDR:?}" "${TS_UP_TIMEOUT:?}"

SOCK=/tmp/tailscaled.sock
STATEDIR=/tmp/tailscale
mkdir -p "$STATEDIR"

# Move the key into a 0600 file before anything is started, so neither
# tailscaled's environment nor any process argument list carries it.
KEYFILE=$(mktemp)
chmod 600 "$KEYFILE"
printf '%s' "$TS_AUTHKEY" >"$KEYFILE"
unset TS_AUTHKEY

# Fargate has no /dev/net/tun and no NET_ADMIN, so userspace networking is the
# only mode that runs. In it, traffic arriving over the tailnet for an
# advertised route is re-originated from this task's own address, which is why
# the rule this module adds to the RDS security group admits the router group.
tailscaled \
  --tun=userspace-networking \
  --state="$TS_STATE_ARN" \
  --statedir="$STATEDIR" \
  --socket="$SOCK" \
  --socks5-server="$TS_SOCKS5_ADDR" &
TAILSCALED_PID=$!

# Stop tailscaled with the container so ECS sees one exit, not an orphan.
# Wait for tailscaled to exit so its last state write to SSM is not cut off.
trap 'rm -f "$KEYFILE"; kill "$TAILSCALED_PID" 2>/dev/null || true; wait "$TAILSCALED_PID" 2>/dev/null || true; exit 143' INT TERM

i=0
while [ ! -S "$SOCK" ]; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then
    echo "router: tailscaled socket did not appear within 60s" >&2
    rm -f "$KEYFILE"
    exit 1
  fi
  if ! kill -0 "$TAILSCALED_PID" 2>/dev/null; then
    echo "router: tailscaled exited before its socket appeared" >&2
    rm -f "$KEYFILE"
    exit 1
  fi
  sleep 1
done

# --accept-routes=false: the router reaches the tailnet through SOCKS5 only and
#   must not install other nodes' subnets.
# --accept-dns=false: userspace mode has no resolver to configure; the SOCKS5
#   proxy resolves tailnet names itself.
if ! tailscale --socket="$SOCK" up \
  --auth-key="file:$KEYFILE" \
  --hostname="$TS_HOSTNAME" \
  --advertise-routes="$TS_ROUTES" \
  --accept-routes=false \
  --accept-dns=false \
  --timeout="$TS_UP_TIMEOUT"; then
  rm -f "$KEYFILE"
  echo "router: tailscale up failed" >&2
  exit 1
fi
rm -f "$KEYFILE"

echo "router: up; advertising $TS_ROUTES"
wait "$TAILSCALED_PID"
