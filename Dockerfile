# GISGP MCP is a remote server (Streamable HTTP at https://gisgp.com/mcp).
# This Dockerfile makes it runnable as a local stdio server for MCP
# clients/registries that only support the stdio transport, using the
# standard mcp-remote bridge (https://github.com/geelen/mcp-remote) —
# no GISGP application code lives in this container, it only proxies
# requests to the real remote endpoint.

FROM node:20-alpine

ENTRYPOINT ["npx", "-y", "mcp-remote", "https://gisgp.com/mcp"]
