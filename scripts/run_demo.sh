#!/usr/bin/env bash
# Preflight demo runner (arbiter-owned).
#   scripts/run_demo.sh dev     — local DEV_MODE stack: API + paying-client --dev + tests
#   scripts/run_demo.sh prod     — show the LIVE 402 of the production deployment
#   scripts/run_demo.sh tests    — offline test suite only
# PAID flows against production spend REAL USDC (Base mainnet) — not run here.
set -euo pipefail
cd "$(dirname "$0")/.."

MODE="${1:-dev}"
export PREFLIGHT_PORT="${PREFLIGHT_PORT:-8000}"
API="http://127.0.0.1:${PREFLIGHT_PORT}"
PROD="https://preflight--preflight-api-api.us-east.modal.run"

if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

run_tests() {
  echo "=== offline suite ==="
  uv run pytest -q -m "not network"
}

case "$MODE" in
  tests)
    run_tests
    ;;
  dev)
    export DEV_MODE=1
    echo "=== starting local API (DEV_MODE) on :${PREFLIGHT_PORT} ==="
    uv run uvicorn preflight.api.app:app --host 127.0.0.1 --port "${PREFLIGHT_PORT}" --log-level warning &
    API_PID=$!
    trap 'kill $API_PID 2>/dev/null || true' EXIT
    for _ in $(seq 1 40); do curl -sf "${API}/health" >/dev/null 2>&1 && break; sleep 0.5; done
    curl -sf "${API}/health" >/dev/null || { echo "API failed to start"; exit 1; }
    echo "=== paying client (dev bypass — no wallet) ==="
    uv run python client/pay_client.py --dev
    echo "=== deep sim (dev bypass) ==="
    uv run python client/pay_client.py --route sim --dev || true
    run_tests
    echo "=== demo complete; API runs until Ctrl-C ==="
    wait $API_PID
    ;;
  prod)
    echo "=== production health ==="
    curl -s "${PROD}/health"; echo
    echo "=== production 402 (unpaid probe — the x402 challenge, real USDC on Base) ==="
    curl -s -i -X POST "${PROD}/v1/scan" -H "Content-Type: application/json" \
      -d '{"source":"contract A {}"}' | grep -E "^HTTP|payment-required" | cut -c1-90
    echo "=== production metrics ==="
    curl -s "${PROD}/metrics" | python3 -m json.tool | head -18
    ;;
  *)
    echo "unknown mode: $MODE (use dev|prod|tests)"; exit 2 ;;
esac
