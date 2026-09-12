# AGENTS.md

Canonical agent instructions for asu-createai-fallback-harness. Read by Codex, Cursor and Kiro directly;
by Claude Code via CLAUDE.md and by Agy / Antigravity via GEMINI.md.

Edit this file only — the adapters beside it just import it.

## What this project is

Two loopback HTTP services that sit between a coding agent and its provider. They relay Claude
Code traffic to `api.anthropic.com` and Codex traffic to `chatgpt.com/backend-api/codex`
untouched, and — only when they recognize a usage-limit error — continue the same turn on ASU
CreateAI's OpenAI-compatible Chat Completions API instead of failing.

It is not a general LLM proxy, not a load balancer, and not a way around anyone's terms. It has
no router for "cheapest model", no request caching, and no queue. Everything it does exists to
make one interrupted turn survive a quota wall.

## Commands

```sh
python3 -m unittest discover -p 'test_*.py' -v      # offline suite, no network, no API calls
python3 -m unittest test_claude_bridge.EventTest -v # one module or class
RUN_CODEX_INTEGRATION=1 python3 -m unittest -v      # additionally drives the real Codex CLI
python3 claude_asu.py --doctor                      # live CreateAI check (spends a little quota)
python3 codex_asu.py --doctor
python3 setup_claude_macos.py status                # what is installed and which provider is live
```

There is no build step, no linter config and no dependency file: **standard library only**, and
it must stay that way. Adding a third-party import is a design change, not a convenience.

## Architecture

Each client gets a translator plus a router, sharing one CreateAI client and one model map.

| Layer | Claude Code | Codex |
|---|---|---|
| Wire format in | Anthropic Messages | OpenAI Responses |
| Translator | `anthropic_bridge.py` | `bridge.py` |
| Primary relay + failover | `claude_router.py` | `router.py` |
| Background service | `claude_daemon.py` (41118) | `daemon.py` (41117) |
| Installer | `setup_claude_macos.py` | `setup_macos.py` |

`bridge.py` also holds the shared pieces: `Upstream` (the CreateAI client, with 5xx retry),
`BridgeError`, and SSE parsing. `model_map.py` resolves a requested model to its CreateAI
counterpart. `keychain.py` reads and writes the token through Security.framework via ctypes.

Request flow: client → loopback server → primary provider verbatim. On a recognized quota error
the router switches that same in-flight request to CreateAI, translating the conversation, the
tool definitions and every completed tool result, and keeps using CreateAI until the provider's
own reset window expires.

## Rules that are not obvious from the code

- **Never log or print request bodies, responses, or credentials.** Logs carry switch events,
  HTTP status and the provider's own error message. The CreateAI token must never reach
  Anthropic or OpenAI, never be written into client config or a LaunchAgent plist, and never
  appear in `argv`.
- **Quota detection must stay narrow.** Only a recognized usage-limit error may switch
  providers. Transient rate limits, `overloaded_error`, 401/403 and network failures are the
  client's to handle and are relayed untouched. Widening this silently spends the user's
  CreateAI budget on errors that would have resolved themselves.
- **Never dispatch a half-parsed tool call.** Tool calls are buffered until the upstream stream
  completes and the arguments parse; anything else raises instead of handing the client a tool
  call the model did not finish.
- **Retries happen only before bytes reach the client.** `Upstream.open` retries 5xx because
  nothing has been streamed at that point. Do not add a retry after streaming has started.
- **CreateAI's two model families reject opposite things** (measured 3/3 each way):
  `aws/claude*` answers `tool_choice: "none"` with HTTP 500; `openai/gpt*` answers a forced
  single-tool choice with HTTP 500. Both reject a conversation that replays tool calls without
  re-declaring those tools. `model_map.accepts_forced_tool` / `accepts_tool_choice_none` encode
  this; re-measure before changing them.
- **The installers must verify before they touch client configuration.** Both run live CreateAI
  checks and a health check against the started service, and leave `settings.json` /
  `config.toml` alone if anything fails. Keep that order.
- **A LaunchAgent's interpreter is part of the Keychain ACL.** The item trusts binaries, so both
  agents must use the same `interpreter()` result or macOS prompts the user for the token on
  every service start.
- **Claude Code cannot start if the service is down**, so `claude_daemon.py` starts serving
  before the token is readable and loads it in a retry loop. Never make startup depend on the
  Keychain.

## Verification expectations

Client-visible behavior is verified against the real clients, not only unit tests: drive
`claude -p` / `codex exec` through a bridge, force fallback with
`touch ~/.claude/asu-fallback-force`, and confirm a tool actually executes. Claims about what
CreateAI accepts come from repeated live probes (3 runs per case), because its 5xx responses are
intermittent and a single failure proves nothing.

After changing code that a LaunchAgent runs, restart it — a running service keeps the old code:

```sh
launchctl kickstart -k gui/$(id -u)/com.rich.asu-claude-bridge
```

## Repository

Public: https://github.com/Rich627/asu-createai-fallback-harness (MIT). CI runs the offline
suite on macOS and Linux across Python 3.9, 3.12 and 3.14 — 3.9 support is real, it is what
`/usr/bin/python3` provides when Homebrew's python is missing.
