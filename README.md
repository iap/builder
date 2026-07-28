# builder — AWS Builder ID for Hermes Agent

> [!IMPORTANT]
> Unofficial, experimental community plugin for the Hermes Agent. Authenticates
> via Amazon Builder ID (AWS BID). Not affiliated with or endorsed by Amazon.

Lets the Hermes Agent talk to **Amazon Q Developer (Claude models)** through a direct HTTPS backend — no external daemon, no subprocess. Hermes drives the agentic loop; this plugin exposes Q as a chat tool and a selectable model. When selected as a model, a lightweight **in-process** OpenAI-compatible adapter (loopback `:8088`) translates between Hermes and Q; it is a thread inside the Hermes session, not a standalone server.

**Repository:** https://github.com/iap/builder.git

---

## Quick start

```bash
# 1. install
hermes plugins install iap/builder

# 2. register as a selectable model
${HERMES_HOME:-$HOME/.hermes}/plugins/builder/scripts/setup.sh

# 3. restart Hermes

# 4. authenticate
bid_login   # approve the user_code in your browser

# 5. use
ask_q prompt="explain recursion"
hermes chat -m aws-builder
```

## Tools

| Tool | Purpose |
|------|---------|
| `ask_q` | Send a prompt to Amazon Q and return the answer. |
| `bid_login` | Start device login; returns `user_code` + verification URL. |
| `bid_status` | Current auth / device-login state. |
| `bid_show_identity` | Token metadata (no raw token). |
| `bid_logout` | Stop polling; delete stored secrets. |
| `models` | List available models and plugin tags. |
| `tags` | List plugin tags. |
| `q_debug` | Auth + identity + render-prefs snapshot (no raw secrets). |

## Two usage paths

- **`ask_q` tool** — chat/reasoning only. Q cannot execute Hermes tools from this path.
- **`-m aws-builder` model** — tool calls fire. The adapter translates `<tool_call>` blocks to OpenAI `tool_calls` frames so Hermes's agentic loop (MCP / skills / native tools) runs.

## Uninstall

```bash
${HERMES_HOME:-$HOME/.hermes}/plugins/builder/scripts/uninstall.sh
hermes plugins uninstall builder
# restart Hermes
```

---

## Documentation

- [Installation & Setup](docs/installation.md)
- [Authentication](docs/authentication.md)
- [Tools Reference](docs/tools.md)
- [Model Catalog](docs/model-catalog.md)
- [Architecture](docs/architecture.md)
- [Testing](docs/testing.md)
- [Contributing](CONTRIBUTING.md)
- [AGENTS.md](AGENTS.md) — guidance for AI coding agents

---

## License

Dual-licensed: **MIT OR Apache-2.0**. See [LICENSE](LICENSE), [LICENSE-MIT](LICENSE-MIT), [LICENSE-APACHE](LICENSE-APACHE).

**Amazon**, **AWS**, **Amazon Q**, and **Builder ID** are trademarks of Amazon.com, Inc. This project is not affiliated with, endorsed by, or sponsored by Amazon.

Copyright © 2026 Iko.
