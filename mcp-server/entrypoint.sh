#!/bin/sh
set -eu

: "${REACHINBOX_API_KEY:?Set REACHINBOX_API_KEY (ReachInbox > Settings > API)}"
: "${MCP_GATEWAY_API_KEY:?Set MCP_GATEWAY_API_KEY (a long random string; n8n sends it as a Bearer token)}"

# Read-only tools only, space separated. Do not add send_onebox_reply here:
# the n8n workflow sends replies itself after validating them.
MCP_TOOLS="${MCP_TOOLS:-list_onebox_threads get_onebox_thread}"

TOOL_FLAGS=""
for t in $MCP_TOOLS; do
  TOOL_FLAGS="$TOOL_FLAGS --tools $t"
done

# shellcheck disable=SC2086
exec supergateway \
  --stdio "node /app/dist/index.js" \
  --outputTransport streamableHttp \
  --port "${PORT:-8000}" \
  --streamableHttpPath /mcp \
  --healthEndpoint /healthz \
  --apiKey "$MCP_GATEWAY_API_KEY" \
  $TOOL_FLAGS
