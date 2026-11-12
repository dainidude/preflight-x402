#!/usr/bin/env bash
# Preflight full-stack demo runner (arbiter-owned).
# Usage:
#   scripts/run_demo.sh dev     — DEV_MODE stack: API + MCP + paying-client --dev + tests
#   scripts/run_demo.sh testnet — real x402 flow on Base Sepolia (needs faucet funding)
#   scripts/run_demo.sh tests   — pytest only
set -euo pipefail
cd "$(dirname "$0")/.."

MODE="${1:-dev}"
export PREFLIGHT_PORT="${PREFLIGHT_PORT:-8000}"
API="http://127.0.0.1:${PREFLIGHT_PORT}"

# load .env if present (never committed)
if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

run_tests() {
  echo "=== pytest ==="
  uv run pytest -q -m "not network" || true
}

start_api() {
  echo "=== starting API (DEV_MODE=$DEV_MODE) on :${PREFLIGHT_PORT} ==="
  uv run uvicorn preflight.api.app:app --host 127.0.0.1 --port "${PREFLIGHT_PORT}" --log-level warning &
  API_PID=$!
  trap 'kill $API_PID 2>/dev/null || true' EXIT
  for _ in $(seq 1 40); do
    curl -sf "${API}/health" >/dev/null 2>&1 && break
    sleep 0.5
  done
  curl -sf "${API}/health" >/dev/null || { echo "API failed to start"; exit 1; }
  echo "API up: ${API}/health"
}

wait_and_report() {
  wait $API_PID
}

case "$MODE" in
  tests)
    run_tests
    ;;
  dev)
    export DEV_MODE=1
    start_api
    echo "=== price list ==="
    curl -s "${API}/" ; echo
    echo "=== paying client (dev bypass) ==="
    uv run python client/pay_client.py --dev
    echo "=== MCP server tool call (dev) ==="
    (echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"demo","version":"0.1"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"preflight_check","arguments":{"source":"contract Bad { mapping(address=>uint256) balances; function withdraw() external { uint256 a = balances[msg.sender]; (bool ok,) = msg.sender.call{value: a}(\u0022\u0022); require(ok); balances[msg.sender] = 0; } }"}}}'; sleep 90) \
      | timeout 120 uv run python -m preflight.mcp.server 2>/dev/null | tail -2 || echo "(MCP stdio call failed — see docs/STATUS_mcp.md)"
    run_tests
    echo "=== demo complete; API left running until Ctrl-C ==="
    wait_and_report
    ;;
  testnet)
    export DEV_MODE=0
    start_api
    echo "=== paying client (real 402 on Base Sepolia) ==="
    uv run python client/pay_client.py
    echo "=== leaving API running until Ctrl-C ==="
    wait_and_report
    ;;
  *)
    echo "unknown mode: $MODE (use dev|testnet|tests)"; exit 2 ;;
esac
