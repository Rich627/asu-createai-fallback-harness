# CreateAI Fallback Harness

Keep working when your coding agent runs out of quota. This is a loopback bridge that
relays [Claude Code](https://claude.com/claude-code) and [OpenAI Codex](https://developers.openai.com/codex/cli)
traffic to their normal providers, and — only when it recognizes a usage-limit error —
continues the *same turn* on [ASU CreateAI](https://ai.asu.edu/ai-tools/createai-platform)
instead of stopping.

繁體中文說明：[README.zh-TW.md](README.zh-TW.md)

```
Claude Code ──► 127.0.0.1:41118 ──► api.anthropic.com                (normal)
                       └──────────► CreateAI /chat/completions       (usage limit reached)

Codex       ──► 127.0.0.1:41117 ──► chatgpt.com/backend-api/codex    (normal)
                       └──────────► CreateAI /chat/completions       (usage limit reached)
```

- **Same session.** The conversation, the tool definitions and every completed tool result
  travel with the switched request. No restart, no re-run of tools that already ran.
- **Only on quota.** A recognized usage-limit error switches providers. Short rate limits,
  `overloaded_error`, auth failures and ordinary task errors are relayed to the client untouched.
- **It switches back.** The fallback lasts as long as the provider's own reset window says,
  so your subscription is used again the moment it is available.
- **Your keys stay put.** The client's own credentials are relayed only to that client's own
  provider. The CreateAI token lives in the macOS Keychain, is read by the background service,
  and is never written into Claude Code or Codex configuration.
- **Standard library only.** No third-party Python packages anywhere in the runtime.

## Status

| Path | State |
|---|---|
| Claude Code → CreateAI translation, streaming, tool calls | verified end to end on macOS |
| Claude Code usage-limit detection | rule-based, **not yet observed against a live Anthropic 429** |
| Codex → CreateAI translation, streaming, tool calls | verified end to end |
| Codex usage-limit detection | **verified against a genuinely exhausted ChatGPT quota** |
| Anything but macOS | the bridges are portable; the installers are not |

## Requirements

- macOS (Keychain for the token, a per-user LaunchAgent for autostart)
- Python 3.9+ (3.9 through 3.14 are covered by CI)
- Claude Code and/or Codex CLI, already logged in
- An ASU CreateAI Builder project and a **Service** API token

### Getting the CreateAI token

1. In your CreateAI Builder project open **Profile → API Keys → Request API Key**.
2. Set **Key Type** to `Service` and give it a name.
3. Describe the use honestly, for example: *a local API-compatibility bridge that lets my
   coding assistant continue an interrupted task on my CreateAI project when my primary
   provider reaches its usage limit; the service token stays on my machine.*
4. Keep the token out of shell history and source control — the installers read it from a
   hidden prompt and store it in the Keychain.

## Install

Both installers test CreateAI **before** touching any client configuration, and refuse to
change anything if those tests fail.

### Claude Code

```sh
python3 setup_claude_macos.py install
python3 setup_claude_macos.py status      # also prints which provider is live right now
python3 setup_claude_macos.py uninstall
```

This installs the LaunchAgent `com.rich.asu-claude-bridge` on `127.0.0.1:41118` and sets
`env.ANTHROPIC_BASE_URL` in `~/.claude/settings.json` (backed up as `settings.json.asu-backup-*`).
Start a new Claude Code session afterwards.

### Codex

```sh
python3 setup_macos.py install
python3 setup_macos.py status
python3 setup_macos.py uninstall
```

This installs the LaunchAgent `com.rich.asu-codex-bridge` on `127.0.0.1:41117` and adds a
`model_provider` to `~/.codex/config.toml` (backed up as `config.toml.asu-backup-*`).
Fully quit and reopen Codex afterwards.

### Without installing anything

```sh
python3 claude_asu.py --doctor              # live CreateAI check for the Claude path
python3 claude_asu.py -- -p "hello"         # run Claude Code through a temporary bridge
python3 codex_asu.py --doctor               # live CreateAI check for the Codex path
python3 codex_asu.py --auto -- exec "hi"    # run Codex through a temporary bridge
```

## Model mapping

Both clients let you switch models (`/model`), and that choice is honored: the requested model
is mapped to its CreateAI counterpart, looked up in the live model list, so new CreateAI models
work without a code change.

| You picked | CreateAI model used during fallback |
|---|---|
| `claude-opus-5` | `aws/claude5_opus` |
| `claude-sonnet-5` | `aws/claude5_sonnet` |
| `claude-haiku-4-5-*` | `aws/claude4_5_haiku` |
| `claude-opus-4-1-*` | `aws/claude4_1_opus` |
| `gpt-5.6-sol` | `openai/gpt5_6_sol` |
| `gpt-6-astra` | `openai/gpt6_astra` |
| anything with no counterpart | newest model of the same family, else the configured default |

Pass `--model <exact CreateAI id>` to either installer to pin every fallback request instead.

## When it switches

It switches when the primary returns a usage-limit error:

- Anthropic: HTTP 402/429 with `anthropic-ratelimit-unified-status: rejected`, or an error
  message about a usage limit, quota or credit balance.
- ChatGPT/Codex: HTTP 402/429 with `insufficient_quota`, `usage_limit_reached`,
  `quota_exceeded` or `billing_hard_limit_reached`.

It does **not** switch on transient rate limits, `overloaded_error`, 401/403, network failures,
or the model simply failing a task. Unrecognized errors are relayed with their type and message
so a missed pattern is diagnosable from the client's own output.

The window comes from `anthropic-ratelimit-unified-reset` or `retry-after` when present,
otherwise 30 minutes, capped at 6 hours. To force fallback for a test:
`touch ~/.claude/asu-fallback-force` (delete it to go back).

## What the fallback cannot carry

CreateAI is an OpenAI-compatible Chat Completions API, so while it is serving:

- no extended thinking, no prompt caching, and no provider-hosted tools (web search, code
  execution, image generation) — those fields are dropped, ordinary tool calls are kept;
- thinking blocks produced earlier by Anthropic are not forwarded (their signatures are not
  transferable); visible messages and tool results are;
- MCP tool names longer than 64 characters are hashed on the wire and restored on the way back;
- CreateAI's two model families reject opposite things (measured 3/3 each way): Bedrock-hosted
  `aws/claude*` answers `tool_choice: "none"` with HTTP 500, while OpenAI-hosted `openai/gpt*`
  answers a forced single-tool choice with HTTP 500. A conversation that replays tool calls must
  also re-declare those tools. The bridge handles all three;
- CreateAI also returns intermittent 5xx, so every upstream call is retried up to three times
  before the client's turn is allowed to fail;
- token counting is estimated;
- your CreateAI project's own quota and its 750k tokens/minute rate limit now apply, and each
  fallback request is billed to that project.

## Security notes

- Both services bind to `127.0.0.1` only and reject any request carrying an `Origin` header.
- The CreateAI token is never forwarded to Anthropic or OpenAI, never written to client config
  or to the LaunchAgent plist, and never printed.
- Requests, responses and credentials are not logged. The log records switch events, the HTTP
  status and the provider's own error message.
- During fallback your conversation, code excerpts and tool output are sent to CreateAI and
  handled under that project's terms.

## Escape hatch

If the bridge is down, every client session pointed at it fails. To bypass it immediately:

```sh
ANTHROPIC_BASE_URL= claude           # one session
python3 setup_claude_macos.py uninstall   # permanently, restores the previous settings
```

The LaunchAgents use `KeepAlive`, so a crashed service is restarted by macOS within seconds,
and the Claude service starts even before its Keychain token is readable so a locked Keychain
cannot take the client offline.

## Tests

```sh
python3 -m unittest discover -p 'test_*.py' -v      # offline, no network, no API calls
RUN_CODEX_INTEGRATION=1 python3 -m unittest -v      # additionally drives the real Codex CLI
```

CI runs the offline suite on macOS and Linux across Python 3.9, 3.12 and 3.14.

## Layout

| File | Purpose |
|---|---|
| `bridge.py` | Responses → CreateAI translation and the local server (Codex) |
| `router.py` | Codex primary relay with usage-limit failover |
| `anthropic_bridge.py` | Messages → CreateAI translation (Claude Code) |
| `claude_router.py` | Claude primary relay with usage-limit failover |
| `model_map.py` | Requested model → CreateAI model resolution |
| `keychain.py` | Native macOS Keychain access, no secret in `argv` |
| `daemon.py`, `claude_daemon.py` | The two background services |
| `setup_macos.py`, `setup_claude_macos.py` | Install, status, uninstall |
| `codex_asu.py`, `claude_asu.py` | One-off runs and live checks |

## License

MIT — see [LICENSE](LICENSE). Not affiliated with Anthropic, OpenAI, or Arizona State University.
