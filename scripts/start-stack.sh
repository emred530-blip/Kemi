#!/usr/bin/env bash
# Start a complete Kemi stack on one machine:
#
#   1. a bootstrap node        — the address others join through
#   2. a provider node         — serves compute and AI, earns credits
#   3. a public access portal  — the web app people use (with /admin console)
#   4. an OpenAI-compatible gateway — point existing AI tools at the network
#
# Everything is a normal `kemi` process; none of them is privileged. Stop the
# whole stack with: scripts/start-stack.sh stop
#
# Environment overrides:
#   BOOTSTRAP_PORT (7700)  PROVIDER_PORT (7701)  PORTAL_PORT (8090)
#   GATEWAY_PORT (11434)   DASHBOARD_PORT (8080) FAUCET (10)
#   AI_BACKEND (mock)      AI_MODEL ("")         ADMIN_KEY (generated)
#   STACK_HOME (~/.kemi-stack)  BIND (127.0.0.1, use 0.0.0.0 to publish)

set -euo pipefail

BOOTSTRAP_PORT="${BOOTSTRAP_PORT:-7700}"
PROVIDER_PORT="${PROVIDER_PORT:-7701}"
PORTAL_PORT="${PORTAL_PORT:-8090}"
GATEWAY_PORT="${GATEWAY_PORT:-11434}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8080}"
FAUCET="${FAUCET:-10}"
AI_BACKEND="${AI_BACKEND:-mock}"
AI_MODEL="${AI_MODEL:-}"
STACK_HOME="${STACK_HOME:-$HOME/.kemi-stack}"
BIND="${BIND:-127.0.0.1}"
RUN_DIR="$STACK_HOME/run"

kemi_bin() {
  if command -v kemi >/dev/null 2>&1; then echo "kemi"; else echo "python3 -m kemi"; fi
}
KEMI="$(kemi_bin)"

stop_stack() {
  local stopped=0
  for pidfile in "$RUN_DIR"/*.pid; do
    [ -e "$pidfile" ] || continue
    local pid; pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then kill "$pid" 2>/dev/null && stopped=$((stopped + 1)); fi
    rm -f "$pidfile"
  done
  echo "stopped $stopped process(es)"
}

wait_for_port() {  # host port timeout_seconds
  local deadline=$(( SECONDS + $3 ))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if python3 - "$1" "$2" <<'PY' 2>/dev/null
import socket, sys
s = socket.socket()
s.settimeout(1)
sys.exit(0 if s.connect_ex((sys.argv[1], int(sys.argv[2]))) == 0 else 1)
PY
    then return 0; fi
    sleep 0.5
  done
  return 1
}

start_one() {  # name  home  command...
  local name="$1" home="$2"; shift 2
  mkdir -p "$home" "$RUN_DIR"
  KEMI_HOME="$home" nohup "$@" >"$RUN_DIR/$name.log" 2>&1 &
  echo $! >"$RUN_DIR/$name.pid"
}

if [ "${1:-start}" = "stop" ]; then stop_stack; exit 0; fi

mkdir -p "$RUN_DIR"
stop_stack >/dev/null 2>&1 || true

ADMIN_KEY="${ADMIN_KEY:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')}"
AI_ARGS=(--ai-backend "$AI_BACKEND")
[ -n "$AI_MODEL" ] && AI_ARGS+=(--ai-model "$AI_MODEL")

echo "Starting Kemi stack (state under $STACK_HOME)"

start_one bootstrap "$STACK_HOME/bootstrap" \
  $KEMI node --host "$BIND" --port "$BOOTSTRAP_PORT"
wait_for_port "$BIND" "$BOOTSTRAP_PORT" 30 \
  || { echo "bootstrap node failed to bind; see $RUN_DIR/bootstrap.log"; exit 1; }
echo "  [1/4] bootstrap node   $BIND:$BOOTSTRAP_PORT"

start_one provider "$STACK_HOME/provider" \
  $KEMI node --host "$BIND" --port "$PROVIDER_PORT" --provide \
  --peer "$BIND:$BOOTSTRAP_PORT" --ui "$DASHBOARD_PORT" "${AI_ARGS[@]}"
wait_for_port "$BIND" "$PROVIDER_PORT" 30 \
  || { echo "provider node failed to bind; see $RUN_DIR/provider.log"; exit 1; }
echo "  [2/4] provider node    $BIND:$PROVIDER_PORT  (backend: $AI_BACKEND${AI_MODEL:+/$AI_MODEL})"

start_one portal "$STACK_HOME/portal" \
  $KEMI web --peer "$BIND:$BOOTSTRAP_PORT" --web-host "$BIND" \
  --web-port "$PORTAL_PORT" --faucet "$FAUCET" --admin-key "$ADMIN_KEY"
wait_for_port "$BIND" "$PORTAL_PORT" 30 \
  || { echo "access portal failed to bind; see $RUN_DIR/portal.log"; exit 1; }
echo "  [3/4] access portal    $BIND:$PORTAL_PORT"

start_one gateway "$STACK_HOME/gateway" \
  $KEMI serve --peer "$BIND:$BOOTSTRAP_PORT" --host "$BIND" --serve-port "$GATEWAY_PORT"
wait_for_port "$BIND" "$GATEWAY_PORT" 30 \
  || { echo "gateway failed to bind; see $RUN_DIR/gateway.log"; exit 1; }
echo "  [4/4] OpenAI gateway   $BIND:$GATEWAY_PORT"

cat <<EOF

Kemi is running.

  Access portal      http://$BIND:$PORTAL_PORT/
  Operator console   http://$BIND:$PORTAL_PORT/admin
  Admin key          $ADMIN_KEY
  Node dashboard     http://$BIND:$DASHBOARD_PORT/
  OpenAI endpoint    http://$BIND:$GATEWAY_PORT/v1

Use it from any OpenAI-compatible tool:
  export OPENAI_BASE_URL=http://$BIND:$GATEWAY_PORT/v1
  export OPENAI_API_KEY=kemi

Logs: $RUN_DIR/*.log     Stop everything: scripts/start-stack.sh stop
EOF
