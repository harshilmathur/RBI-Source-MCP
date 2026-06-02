# Connect rbi-source-mcp to your MCP client

This is the long form of the table in [README.md](../README.md#connect-to-your-mcp-client). The four most common clients are documented there; this file covers everything else.

All snippets below point at the maintainer's hosted instance at `https://rbi-source.harshil.ai/mcp/`. To use your own self-host stdio install instead, replace the URL block with `"command": "rbi-source-mcp"` (the binary installed by `pip install rbi-source-mcp`).

## Cline (VSCode)

In MCP Settings:

```json
{
  "mcpServers": {
    "rbi-source": {
      "type": "streamableHttp",
      "url": "https://rbi-source.harshil.ai/mcp/"
    }
  }
}
```

## Continue.dev

In `~/.continue/config.json`:

```json
{
  "experimental": {
    "modelContextProtocolServers": [
      {
        "transport": {
          "type": "streamable-http",
          "url": "https://rbi-source.harshil.ai/mcp/"
        }
      }
    ]
  }
}
```

## Goose (Block)

In `~/.config/goose/config.yaml`:

```yaml
extensions:
  rbi-source:
    type: streamable_http
    uri: https://rbi-source.harshil.ai/mcp/
    enabled: true
```

## Zed

In `~/.config/zed/settings.json`:

```json
{
  "context_servers": {
    "rbi-source": {
      "command": null,
      "settings": {
        "url": "https://rbi-source.harshil.ai/mcp/",
        "type": "http"
      }
    }
  }
}
```

## Claude Cowork

Settings → **Connectors** → **Add custom connector** → paste `https://rbi-source.harshil.ai/mcp/`.

## Anything else that speaks MCP streamable-HTTP

Point it at `https://rbi-source.harshil.ai/mcp/`. The endpoint is plain MCP streamable-HTTP; no auth, no protocol extensions. If a client config you'd like to see documented here isn't, open an issue with the format your client expects.

## Verify the connection

```bash
curl -sS -X POST https://rbi-source.harshil.ai/mcp/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | head -c 500
```

Should return an SSE event listing the four tools.

For a self-host stdio install, the equivalent verification is `rbi-source-doctor`.
