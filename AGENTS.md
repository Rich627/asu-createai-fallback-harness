# AGENTS.md

Canonical agent instructions for asu-unlimited-tokens. Read by Codex, Cursor and Kiro directly;
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
# Always from the repository root: `asu` and the entry-point scripts must both import.
python3 -m unittest discover -s tests -t . -p 'test_*.py' -v   # offline, no network, no API calls
python3 -m unittest tests.test_claude_bridge.EventTest -v      # one module or class
RUN_CODEX_INTEGRATION=1 python3 -m unittest discover -s tests -t . -v  # also drives the real Codex CLI
python3 claude_asu.py --doctor                      # live CreateAI check (spends a little quota)
python3 codex_asu.py --doctor
python3 setup_claude_macos.py status                # what is installed and which provider is live
python setup_claude_windows.py status               # the Windows twin; same subcommands
```

There is no build step, no linter config and no dependency file: **standard library only**, and
it must stay that way. Adding a third-party import is a design change, not a convenience.

## Architecture

Each client gets a translator plus a router, sharing one CreateAI client and one model map.

| Layer | Claude Code | Codex |
|---|---|---|
| Wire format in | Anthropic Messages | OpenAI Responses |
| Translator | `asu/anthropic_bridge.py` | `asu/codex_bridge.py` |
| Primary relay + failover | `asu/claude_router.py` | `asu/codex_router.py` |
| Background service | `claude_daemon.py` (41118) | `codex_daemon.py` (41117) |
| Installer (macOS) | `setup_claude_macos.py` | `setup_codex_macos.py` |
| Installer (Windows) | `setup_claude_windows.py` | `setup_codex_windows.py` |

Everything above lives in the `asu` package; the repository root holds only the eight entry
points (two daemons, four installers, two one-off tools). **The dependency runs one way: an entry
point may import from `asu`, nothing in `asu` may import an entry point.** That keeps the package
importable on its own, and it is why `ENVIRONMENTS`, `diagnose` and `doctor` still live in
`codex_asu.py` / `claude_asu.py` at the root rather than being pulled inward — moving them is a
redesign of those files, not a file move. The daemons deliberately keep their root filenames: an
installed LaunchAgent stores an absolute path to them, so renaming or moving one breaks every
existing install until the installer is re-run.

`asu/createai.py` is the shared floor under both columns: `Upstream` (the CreateAI client, with 5xx
retry), `BridgeError`, `dumps`, `NoRedirect` and SSE parsing. Nothing client-specific belongs in
it — if a change to `asu/createai.py` only makes sense for one of the two clients, it is in the wrong
file. `asu/model_map.py` resolves a requested model to its CreateAI counterpart. `asu/keychain.py` reads
and writes the token through Security.framework via ctypes.

Platform integration is the other axis, and it is deliberately thin — only two things actually
differ per platform:

| | macOS | Windows |
|---|---|---|
| Credential store | `asu/keychain.py` (Security.framework) | `asu/credvault.py` (advapi32) |
| Autostart | LaunchAgent plist | scheduled task, `asu/winservice.py` |

`asu/credstore.py` picks the credential backend, so nothing above it branches on `sys.platform`;
both backends import safely anywhere and refuse to act off their own platform. `asu/installer.py`
holds what every installer shares (atomic writes, the health wait, the token round-trip check)
and `asu/codex_config.py` holds the `config.toml` editing, so a fix lands once instead of four times.

The two clients each keep their own `ToolMap` (`anthropic_bridge` keys by `by_name`,
`codex_bridge` by `by_original` and supports namespacing). They are deliberately not merged;
they encode different wire shapes, and both are covered by their own tests.

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
  every service start. This is macOS-only: a Credential Manager entry belongs to the user, not
  to a trusted binary, so `winservice.interpreter()` is free to prefer `pythonw.exe` and to
  change between installs.
- **A scheduled task needs `ExecutionTimeLimit` of `PT0S`.** The Task Scheduler default stops a
  task after 72 hours, which would take the bridge down and leave the client pointed at a dead
  port. `RestartOnFailure` is the `KeepAlive` equivalent, and `Hidden` plus `pythonw.exe` is what
  keeps a console window off the user's screen. The task is defined as XML because `schtasks
  /TR` cannot express any of this; the file must be UTF-16, which is what `write_task_xml` does.
- **`pythonw.exe` has no console, so the daemons take `--log`.** A LaunchAgent redirects stdout
  for us and a scheduled task has no equivalent. Do not replace this with shell redirection in
  the task action — that reintroduces a console window and depends on how the interpreter was
  launched.
- **Claude Code cannot start if the service is down**, so `claude_daemon.py` starts serving
  before the token is readable and loads it in a retry loop. Never make startup depend on the
  Keychain.
- **The managed-block marker is written into the user's `config.toml`.** `codex_config` finds
  its block by `BEGIN_PREFIX` and writes the longer `BEGIN`, so a block left by an older version
  is still recognized and removed. Never match on the full marker: doing so orphans every block
  written before the text last changed, and the text has already changed twice.
  `tests/test_codex_config.py` pins both historical spellings.

## Verification expectations

Client-visible behavior is verified against the real clients, not only unit tests: drive
`claude -p` / `codex exec` through a bridge and confirm a tool actually executes. Claims about
what CreateAI accepts come from repeated live probes (3 runs per case), because its 5xx
responses are intermittent and a single failure proves nothing.

Forcing fallback exists only on the Claude side, in two forms that are not interchangeable:
`touch ~/.claude/asu-fallback-force` is machine-wide and diverts every running Claude bridge,
while `claude_asu.py --force-fallback` forces that one instance and never writes the flag file.
`router.py` has no force path at all — it switches only on a real `PrimaryQuota`, so exercising
Codex fallback means hitting an actual usage limit or stubbing the primary.

Windows is the exception to all of that, and the gap is recorded in the README's Status table
rather than papered over. CI on `windows-latest` genuinely exercises the Credential Manager
round trip against real `advapi32` and a real `schtasks` create/query/delete, and the task
definition is asserted field by field — but nobody has confirmed that the logon trigger brings
the bridge up on a desktop, or driven Claude Code or Codex through it on Windows. Do not promote
those rows to "verified" on the strength of a green CI run.

After changing code that a LaunchAgent runs, restart it — a running service keeps the old code:

```sh
launchctl kickstart -k gui/$(id -u)/com.rich.asu-claude-bridge
```

## Repository

Public: https://github.com/Rich627/asu-unlimited-tokens (MIT). CI runs the offline
suite on macOS, Linux and Windows across Python 3.9, 3.12 and 3.14 — 3.9 support is real, it is
what `/usr/bin/python3` provides when Homebrew's python is missing. The Windows-only tests skip
on every other platform, so a green local run says nothing about them.
