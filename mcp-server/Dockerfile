# ReachInbox MCP server exposed over Streamable HTTP for n8n.
#
# - MCP server: community project github.com/nbhr-maker/reachinbox-mcp (Unlicense),
#   pinned to a reviewed commit. It speaks MCP over stdio.
# - supergateway turns stdio into Streamable HTTP, requires an API key on every
#   request, and exposes ONLY the read-only Onebox tools listed in MCP_TOOLS.
#   Nothing that sends email, launches campaigns or deletes data is reachable.

FROM node:22-alpine AS build
RUN apk add --no-cache git
ARG REACHINBOX_MCP_REPO=https://github.com/nbhr-maker/reachinbox-mcp.git
ARG REACHINBOX_MCP_COMMIT=7068466deaf55c1e4a4ae1417bb06990535030eb
WORKDIR /src
RUN git clone "$REACHINBOX_MCP_REPO" . \
 && git checkout "$REACHINBOX_MCP_COMMIT" \
 && npm ci \
 && npm run build \
 && npm prune --omit=dev

FROM node:22-alpine
ARG SUPERGATEWAY_VERSION=4.2.1
RUN npm install -g "supergateway@${SUPERGATEWAY_VERSION}" && npm cache clean --force
WORKDIR /app
COPY --from=build /src/dist ./dist
COPY --from=build /src/node_modules ./node_modules
COPY --from=build /src/package.json ./package.json
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
USER node
EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
