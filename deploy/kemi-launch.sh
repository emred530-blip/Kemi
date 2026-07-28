#!/usr/bin/env bash
# Launcher used by the systemd units. Its whole job is to turn the settings in
# /etc/kemi/kemi.env into a command line — systemd cannot express "pass this
# flag only when the variable is set", and several of Kemi's flags are optional
# or repeatable.
#
#   kemi-launch node | portal | gateway
set -euo pipefail

KEMI_BIN="${KEMI_BIN:-/opt/kemi/venv/bin/kemi}"
PORT="${KEMI_PORT:-7700}"
PORTAL_PORT="${KEMI_PORTAL_PORT:-8090}"
GATEWAY_PORT="${KEMI_GATEWAY_PORT:-11434}"
DASHBOARD_PORT="${KEMI_DASHBOARD_PORT:-8080}"

# KEMI_PEERS is a space- or comma-separated list of host:port entries.
peers="${KEMI_PEERS:-}"
peer_args=()
for peer in ${peers//,/ }; do
  [ -n "$peer" ] && peer_args+=(--peer "$peer")
done

case "${1:-}" in
  node)
    args=(node --provide --host 0.0.0.0 --port "$PORT"
          --price "${KEMI_PRICE:-1.0}"
          --ai-backend "${KEMI_AI_BACKEND:-mock}"
          --ui "$DASHBOARD_PORT" --ui-host 127.0.0.1)
    [ -n "${KEMI_ADVERTISE:-}" ] && args+=(--advertise-host "$KEMI_ADVERTISE")
    [ -n "${KEMI_AI_MODEL:-}" ] && args+=(--ai-model "$KEMI_AI_MODEL")
    args+=("${peer_args[@]+"${peer_args[@]}"}")
    ;;
  portal)
    # The portal joins the node running on this same machine, so it always has
    # at least one provider to reach even before the wider network answers.
    args=(web --web-host 127.0.0.1 --web-port "$PORTAL_PORT"
          --peer "127.0.0.1:$PORT"
          --faucet "${KEMI_FAUCET:-10}"
          --guests-per-ip "${KEMI_GUESTS_PER_IP:-5}"
          --trust-proxy)
    [ -n "${KEMI_ADMIN_KEY:-}" ] && args+=(--admin-key "$KEMI_ADMIN_KEY")
    args+=("${peer_args[@]+"${peer_args[@]}"}")
    ;;
  gateway)
    args=(serve --host 127.0.0.1 --serve-port "$GATEWAY_PORT"
          --peer "127.0.0.1:$PORT")
    args+=("${peer_args[@]+"${peer_args[@]}"}")
    ;;
  *)
    echo "usage: kemi-launch node|portal|gateway" >&2
    exit 2
    ;;
esac

exec "$KEMI_BIN" "${args[@]}"
