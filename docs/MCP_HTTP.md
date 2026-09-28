# brainctl-mcp-http — HTTP transport for the MCP server

brainctl ships two MCP transports:

| Transport | Entry point        | Use case |
|-----------|--------------------|----------|
| stdio     | `brainctl-mcp`     | Claude Desktop, local dev, subprocess-spawned agents. Unchanged. |
| HTTP      | `brainctl-mcp-http` | Remote agents that require Streamable HTTP (xAI Grok remote-MCP, Strand, anything over a network boundary). |

The HTTP transport exposes the **same** `app: Server` instance the stdio
path uses — tools and their handlers are registered once, in
`src/agentmemory/mcp_server.py`. The HTTP module adds transport + auth +
allowlist only; no tool behaviour is duplicated.

## Install

```bash
pip install 'brainctl[mcp]'
```

The `mcp` extra now pulls Starlette, uvicorn (with standard extras),
and python-json-logger in addition to the MCP SDK itself.

## Environment

| Variable                      | Required | Default   | Notes |
|-------------------------------|----------|-----------|-------|
| `BRAINCTL_HTTP_TOKEN`         | yes      | —         | Static bearer token. Must be ≥32 chars. Boot fails loudly otherwise. |
| `BRAINCTL_HTTP_ALLOWED_TOOLS` | no       | visible v2 surface (100 tools) | Comma-separated list of MCP tool names exposed over HTTP. When unset, defaults to the full visible v2 surface — `tools/list` returns the same 100 tools the stdio transport does. When set, must contain only visible v2 tool names; v1-deprecated names (`lc_fire`, `belief_collapse`, etc.) hard-fail at boot with a `docs/TOOL_MIGRATION_V2.md` hint, the same as the stdio `BRAINCTL_ALLOWED_TOOLS`. `tools/call` on any name outside the resolved set returns JSON-RPC `-32601`. Per-request `allowed_tools` from the client (xAI Grok, Strand, etc.) narrows further on top of this. |
| `BRAINCTL_HTTP_PORT`          | no       | `8080`    | TCP port. |
| `BRAINCTL_HTTP_HOST`          | no       | `0.0.0.0` | Bind address. |
| `BRAINCTL_HTTP_LOG_LEVEL`     | no       | `info`    | `debug` / `info` / `warning` / `error` / `critical`. |

Bad config → `exit 1` with a one-line stderr message before logging is
configured.

## Local run

Either via the console script or uvicorn directly:

```bash
# Minimal — defaults to the full 100-tool v2 surface
export BRAINCTL_HTTP_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
brainctl-mcp-http

# Or narrow the surface server-side
export BRAINCTL_HTTP_ALLOWED_TOOLS=memory_search,memory_add,entity_search
brainctl-mcp-http

# Or via uvicorn:
# uvicorn "agentmemory.mcp_http:create_app" --factory --port 8080
```

## Probes

```bash
# Health — no auth.
curl -s http://localhost:8080/health
# → {"ok":true}

# Listing tools — filtered to the allowlist.
curl -s -X POST http://localhost:8080/mcp \
  -H "Authorization: Bearer $BRAINCTL_HTTP_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# Calling an allowlisted tool.
curl -s -X POST http://localhost:8080/mcp \
  -H "Authorization: Bearer $BRAINCTL_HTTP_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"memory_search","arguments":{"query":"foo"}}}'
```

## Deploy

Any container host that speaks HTTP. Example (Fly.io):

```Dockerfile
FROM brainctl:2.4.12
ENV BRAINCTL_HTTP_ALLOWED_TOOLS=memory_search,entity_search
EXPOSE 8080
CMD ["brainctl-mcp-http"]
```

Set `BRAINCTL_HTTP_TOKEN` as a secret in the platform's secret manager
(`fly secrets set`, `gh secret set`, etc.) — never bake it into the
image.

## Connecting to xAI Grok (remote MCP)

xAI's Grok models support remote MCP servers via Streaming HTTP. The
`brainctl-mcp-http` transport is wire-compatible — point Grok at the
deployed URL with the bearer token. Grok-side `allowed_tools` (or
`allowed_tool_names` in the xAI SDK) narrows further on top of
whatever `BRAINCTL_HTTP_ALLOWED_TOOLS` already restricts to.

```python
from xai_sdk import Client
from xai_sdk.chat import user
from xai_sdk.tools import mcp

client = Client(api_key=os.environ["XAI_API_KEY"])
chat = client.chat.create(
    model="grok-4.3",
    tools=[mcp(
        server_url="https://brainctl-mcp.your-domain/mcp",
        authorization=f"Bearer {os.environ['BRAINCTL_HTTP_TOKEN']}",
        # Optional Grok-side narrowing. Omit to let Grok see the
        # full server-exposed surface (which is the 100-tool v2
        # default if you didn't set BRAINCTL_HTTP_ALLOWED_TOOLS):
        allowed_tool_names=[
            "agent_orient", "brainctl_wrapup",
            "memory_add", "memory_search", "vsearch",
            "entity_create", "entity_search", "entity_observe",
            "decision_add", "event_add",
            "subsystem_emit", "subsystem_status",
        ],
    )],
)
chat.append(user("Recap my last brainctl session and what's still open."))
for response, chunk in chat.stream():
    print(chunk.content or "", end="", flush=True)
```

OpenAI-compatible Responses API:

```python
client.responses.create(
    model="grok-4.3",
    input=[{"role": "user", "content": "..."}],
    tools=[{
        "type": "mcp",
        "server_url": "https://brainctl-mcp.your-domain/mcp",
        "server_label": "brainctl",
        "authorization": f"Bearer {os.environ['BRAINCTL_HTTP_TOKEN']}",
    }],
)
```

Spec: <https://docs.x.ai/docs/guides/tools/remote-mcp-tools>. The
server URL must be reachable from the public internet — for local
dev use a tunnel (Cloudflare Tunnel, ngrok, etc.).

## Security notes

* The bearer token is the **only** auth. Do not expose the service
  publicly without TLS (use a reverse proxy — Caddy, nginx, Cloudflare
  Tunnel — or let the host provide HTTPS termination).
* Tool arguments and results are **never** logged; only request id,
  method, tool name, duration, and status are written (JSON).
* Rate limit: 100 req/min per client IP, sliding window, in-memory.
  Multi-node deployments should front the service with an upstream
  limiter — the in-memory limiter doesn't coordinate across workers.
* Request body is capped at 1 MiB. Oversized requests get `413`.
* Graceful shutdown on `SIGTERM` drains in-flight requests for up to
  10s before closing the uvicorn loop.

## Stdio path is unchanged

`brainctl-mcp` still boots the original stdio server — same tool
registration, same dispatch, same code path. The HTTP transport is an
additive sibling, not a rewrite. Existing Claude Desktop users need to
do nothing.
