<p align="center">
  <img src="docs/icon.png" alt="AgentRadio icon" width="96">
</p>
<h2 align="center">AgentRadio</h2>
<p align="center">
  A local message bus for agents running in <a href="https://herdr.dev">Herdr</a> panes.
</p>
<p align="center">
  <img src="https://img.shields.io/badge/version-0.2.7-7dcfff?style=flat-square" alt="version">
  <img src="https://img.shields.io/badge/python-3.8%2B%20stdlib-bb9af7?style=flat-square" alt="python">
  <img src="https://img.shields.io/badge/license-MIT-9ece6a?style=flat-square" alt="license">
  <img src="https://img.shields.io/badge/platform-macos%20%7C%20linux-e0af68?style=flat-square" alt="platform">
</p>

<p align="center">
  <img src="docs/demo.gif" alt="Demo: two bots join by name, one PMs the other, the reply lands back — pushed by the relay">
</p>

One SQLite ledger, one stdlib-only Python CLI, one relay daemon. No servers, no dependencies, no accounts. Agents join by name, get messages pushed straight into their panes, and reply with one-word commands.

PM-only and token-sensitive by design: no rooms, no broadcast, no chatter. A message costs exactly one delivered envelope.

## Why

Several agent CLIs running side by side can't talk to each other. AgentRadio gives them a shared bus:

- **handles, not plumbing** — a handle is just a name bound to a pane
- **push delivery** — the relay drops envelopes into live panes; agents never poll
- **briefing on join** — agents learn the protocol through a system channel, not a wasted first turn
- **payloads by reference** — `--ref /path/to/file` sends a pointer, not pasted text

## Design decisions

AgentRadio is the deliberately reduced form of a richer in-house radio bus that runs inside a multi-agent harness we operate. The reduction is the feature: every mechanism here earns its place by per-token cost, because every briefing and every delivered message lands in an agent's context window.

Deliberately excluded:

- **Rooms / broadcast.** A room message reaches mostly the wrong agents, and every one of them pays tokens for it. DMs only; `radio handles` answers "who is here" without fan-out.
- **Task / handoff tracking.** A shared task board means durable coordination state — ownership, lifecycle, conflict semantics — which roughly doubles the complexity of the bus. A working model of this exists in the harness we reduced from, and we know its failure modes; it may be introduced gradually on a future roadmap, deliberately, not absorbed by default. Until then, handoffs travel as refs: point to a file, keep the message short.
- **MCP transport.** A tool schema costs context in every session permanently; a CLI costs it only when used. Agents already have a shell.
- **Non-herdr agents (plain tmux, SSH).** The bus is a herdr plugin and presence is pane-derived; agents outside herdr are out of scope by design.
- **Multi-machine federation.** One bus per herdr instance. Linking instances is a separate, later question.

## Install

Requires **herdr ≥ 0.9.0** and **python3 ≥ 3.10** — nothing else: the CLI and relay are pure stdlib, and the view's one dependency (Textual) is installed automatically into a plugin-local venv.

```bash
herdr plugin install detailles/AgentRadio   # or: herdr plugin link /path/to/clone
```

The install links `radio` into `~/.local/bin` when that directory exists, and the startup hook repairs that link on every Herdr start — the managed plugin dir is content-hashed and changes on every update, so a link made by hand would dangle. If `~/.local/bin` is not on your PATH, or you want the link elsewhere, point one at the plugin root yourself:

```bash
ROOT="$(herdr plugin list --json | python3 -c 'import json,sys; print(next(p["plugin_root"] for p in json.load(sys.stdin)["result"]["plugins"] if p["plugin_id"]=="radio"))')"
ln -s "$ROOT/bin/radio" ~/.local/bin/radio
```

The relay starts itself via the plugin's startup hook. If the view's venv step is skipped during install (no PyPI access, no pip/uv), the install still succeeds — CLI and relay work, and the view activates later with `sh bin/setup.sh`.

## Quick start

Open two panes, then inside each:

```bash
radio join allocator    # pane 1
radio join janitor      # pane 2
```

From anywhere:

```bash
radio pm janitor 'tmp klasorunu temizle' --from allocator
```

janitor's pane lights up:

```
[RADIO_MESSAGE id=1 kind=pm from=allocator to=janitor reply=not-required]
Radio PM from allocator
tmp klasorunu temizle
[END_RADIO_MESSAGE id=1]
```

No agent CLI? Try the demo bots:

```bash
python3 <plugin_root>/demo/bot.py allocator   # pane 1
python3 <plugin_root>/demo/bot.py assertor    # pane 2
radio pm assertor 'sonucu dogrula' --from allocator
```

## Commands

| Command | What it does |
|---|---|
| `radio join <handle>` | Bind this pane to a handle; launches an agent if you pick one |
| `radio pm <h> 'msg'` | Direct message. `--ref <file>` sends a file reference, `--reply-required` asks for an answer |
| `radio handles` | Who is on: live, gone, or pull |
| `radio inbox` | Index of your messages — ids, senders, status. Nothing consumed |
| `radio show <id>` | Read one exact body; records the delivery |
| `radio log` | Global message log |
| `radio part <h>` | Remove a handle |
| `radio` | Open the dashboard in the current pane |

Sender resolution: `--from`, else `$RADIO_HANDLE`, else the handle bound to the current pane.

## The view

Run one word in any pane — the dashboard adopts and labels it:

```bash
radio
```

Handles, delivery state, pending/failed counts at a glance. The relay is separate, so the view is read-only and can be opened and closed freely.

## Handles and panes

**The pane label IS the handle.** Joining renames the pane to the handle so the two never drift — this is what survives Herdr session restore. The pane's agent session id is recorded too, so a restored agent is matched back to its handle.

Delivery uses `herdr agent prompt`, falling back to `send-text` for plain shells. Handles without a live pane are marked `pull` — their messages wait for `radio inbox` / `radio show <id>`.

Long messages are an anti-pattern: past ~1200 characters the CLI nudges you to write the payload to a file and send `--ref` instead.

## Providers

The bus layer is provider-agnostic. `radio join` can launch an agent CLI into the pane with the briefing injected through a provider-native system channel — never as a radio message, never spending a turn.

| Provider | Briefing channel | Session resume | Status |
|---|---|---|---|
| claude | `--append-system-prompt` | `--resume <id>` | tested |
| codex | `-c developer_instructions=…` | `codex resume <id>` | tested |
| opencode | per-handle config `instructions` | `--session <id>` | tested |
| gemini | SessionStart hook | `--resume <id>` | tested |
| kimi | UserPromptSubmit hook | `--session <id>` | tested |
| qwen | `--append-system-prompt` | `--resume <id>` | written, untested |
| pi | — | `--session <id>` | written, untested |

For gemini and qwen, join assigns the session id itself, so resume is exact.

### Security notes

Radio-launched agents must answer messages unattended, so some providers launch with relaxed approval:

- **gemini / qwen** launch with `--approval-mode yolo` — every tool call in that pane is auto-approved
- **opencode** joins write a per-handle config allowing `--ref` paths outside the working directory
- **codex** launches with per-process `-c` overrides; your global `config.toml` is untouched

These apply only to panes started by `radio join`. Do not point a yolo-mode handle at work you would not auto-approve.

## State

The ledger lives at `$RADIO_HOME/radio.db`, or `~/.local/share/herdr-radio/radio.db` by default. One well-known path per machine: the CLI is invoked from arbitrary panes, so every invocation must resolve to the same ledger.

## Known issues

- **Herdr agent detection vs. bundled CLIs.** Providers that ship as one bundled binary (gemini, qwen) aren't yet recognized as agents by Herdr's pane detection. Consequence: such a handle can show as `gone` while its pane is alive, and delivery falls back to `send-text`. Radio still works; the status column is the casualty. Herdr-side gap, to be fixed there.

## License

[MIT](LICENSE)
