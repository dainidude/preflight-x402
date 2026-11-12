#!/usr/bin/env bash
# Expose the Preflight API publicly via a Cloudflare quick tunnel.
# Prints (and saves to /tmp/opencode/preflight-tunnel.url) the public URL.
# NOTE: quick-tunnel URLs are ephemeral — they die with the process. For a
# permanent launch, use a named Cloudflare tunnel (auth once, DNS route) or
# deploy to Fly.io/Modal per docs/LAUNCH.md.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

PORT="${PREFLIGHT_PORT:-8000}"
URL_FILE="/tmp/opencode/preflight-tunnel.url"

if ! curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
  echo "API not running on :${PORT} — start it first:"
  echo "  DEV_MODE=0 FREE_TIER=1 uv run uvicorn preflight.api.app:app --port ${PORT} &"
  exit 1
fi

echo "Starting quick tunnel -> http://127.0.0.1:${PORT} ..."
cloudflared tunnel --url "http://127.0.0.1:${PORT}" --no-autoupdate > /tmp/opencode/preflight-tunnel.log 2>&1 &
TUNNEL_PID=$!

for _ in $(seq 1 30); do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/opencode/preflight-tunnel.log 2>/dev/null | head -1 || true)
  [[ -n "$URL" ]] && break
  sleep 1
done

if [[ -z "$URL" ]]; then
  echo "Tunnel URL not found — check /tmp/opencode/preflight-tunnel.log"
  kill $TUNNEL_PID 2>/dev/null || true
  exit 1
fi

echo "$URL" | tee "$URL_FILE"
echo "Tunnel PID: $TUNNEL_PID (kill it to tear down)"
echo "Public health check:"
curl -sf "${URL}/health" && echo || echo "(health not reachable yet — give it a few seconds)"
