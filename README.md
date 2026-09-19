# AgentRadio

**A local message bus for agents running in [Herdr](https://herdr.dev) panes.**

One SQLite ledger, one stdlib-only Python CLI, one relay daemon. No servers,
no project dependencies, no accounts. Agents join by name, get messages
pushed straight into their panes, and reply with one-word commands.

PM-only and token-sensitive by design: no rooms, no broadcast, no chatter.
A message costs exactly one delivered envelope — nothing more is spent.

## Why

When you run several agent CLIs side by side, they can't talk to each
other. AgentRadio gives them a shared bus:

- **handles, not plumbing** — a handle is just a name bound to a pane
- **push delivery** — the relay drops envelopes into live panes; agents
  never poll
- **briefing on join** — launched agents learn the protocol through a
  provider-native system channel, not a wasted first turn
- **payloads by reference** — `--ref /path/to/file` sends a pointer, not
  a wall of pasted text

## Install

```bash
herdr plugin install detaybey/AgentRadio   # or: herdr plugin link /path/to/clone
herdr plugin list                          # note the plugin_root for radio
ln -s <plugin_root>/bin/radio ~/.local/bin/radio
```

The relay starts itself via the plugin's startup hook. The CLI is pure
Python 3 stdlib; only the optional view needs its own venv (created by
`bin/setup.sh`, which the plugin runs on install).

## Quick start

Open two panes wherever you like, then inside each:

```bash
radio join bot-a        # in pane 1 — binds the pane, labels it bot-a
radio join bot-b        # in pane 2
```

From anywhere:

```bash
radio pm bot-b 'hello' --from bot-a
```

bot-b's pane lights up with the envelope. If bot-b is an agent, it already
knows how to answer — the join briefing taught it:

```
[RADIO_MESSAGE id=1 kind=pm from=bot-a to=bot-b reply=not-required]
Radio PM from bot-a
hello
[END_RADIO_MESSAGE id=1]
```

Try the demo bots to see a full round-trip without any agent CLI:

```bash
python3 <plugin_root>/demo/bot.py bot-a   # in pane 1 — auto-joins
python3 <plugin_root>/demo/bot.py bot-b   # in pane 2 — auto-replies
radio pm bot-b 'merhaba' --from bot-a
```

## Commands

| Command | What it does |
|---|---|
| `radio join <handle>` | Bind this pane to a handle; launches an agent if you pick one |
| `radio pm <h> 'msg'` | Direct message. `--ref <file>` sends a file reference, `--reply-required` asks for an answer |
| `radio handles` | Who is on: live, gone, or pull |
| `radio inbox` | Metadata index of your messages — ids, senders, status. No bodies, nothing consumed |
| `radio show <id>` | Read one exact body; records the delivery |
| `radio log` | Global message log |
| `radio part <h>` | Remove a handle |
| `radio relay` | Run the relay in the foreground (normally auto-started) |
| `radio` | Open the dashboard in the current pane |

Sender resolution: `--from`, else `$RADIO_HANDLE`, else the handle bound to
the current pane.

## The view

Run one word in any pane you like — the dashboard adopts and labels that
pane itself:

```bash
radio
```

Handles, delivery state, pending/failed counts at a glance. The relay is
separate, so the view is read-only and can be opened and closed freely.

## Handles and panes

**The pane label IS the handle.** Joining renames the pane to the handle so
the two never drift; this is what survives Herdr session restore and what
the UI shows. Joining also records the pane's agent session id into the
handle row, so a restored agent is matched back to its handle.

Delivery uses `herdr agent prompt`, falling back to `send-text` + `enter`
for plain shells. Handles without a live pane are marked `pull` — their
messages wait for `radio inbox` / `radio show <id>`.

Long messages are an anti-pattern: past ~1200 characters the CLI nudges you
to write the payload to a file and send `--ref` instead. Keep envelopes
small; move documents by path.

## Providers

The bus layer is provider-agnostic (panes + handles), but `radio join` can
launch an agent CLI into the pane with the briefing injected through a
provider-native system-context channel — never as a radio message, never
spending a turn.

| Provider | Briefing channel | Session resume | Status |
|---|---|---|---|
| claude | `--append-system-prompt` | `--resume <id>` | tested end-to-end |
| codex | `-c developer_instructions=…` | `codex resume <id>` | tested end-to-end |
| opencode | per-handle config `instructions` | `--session <id>` | tested end-to-end |
| gemini | SessionStart hook (`settings.json`) | `--resume <id>` | tested end-to-end |
| kimi | UserPromptSubmit hook (`config.toml`) | `--session <id>` | tested end-to-end |
| qwen | `--append-system-prompt` | `--resume <id>` | written, not yet live-tested |
| pi | — (no context channel yet) | `--session <id>` | written, not yet live-tested |

For gemini and qwen, join assigns the session id itself (`--session-id`),
so resume is exact instead of a search. Some providers also support named
sessions (e.g. `claude --name <handle>`) — a provider habit, not a Radio
requirement; Radio only ever looks at pane bindings.

### Security notes

Radio-launched agents must answer messages unattended, so some providers
are launched with relaxed approval:

- **gemini and qwen** launch with `--approval-mode yolo`: every tool call
  in that pane is auto-approved for the life of the pane.
- **opencode** joins write a per-handle config under Radio's state dir
  setting `permission.external_directory: allow`, so `--ref` file
  references outside the working directory open without a prompt.
- **codex** launches with per-process `-c` overrides
  (`tui.terminal_title=[]`, `developer_instructions=…`); your global
  `config.toml` is untouched.

These settings apply only to panes started by `radio join`. Interactive
sessions you open yourself keep their own defaults. Do not point a
yolo-mode handle at work you would not auto-approve.

## State

The ledger lives at `$RADIO_HOME/radio.db` when `RADIO_HOME` is set,
otherwise `~/.local/share/herdr-radio/radio.db`. One well-known path per
machine, deliberately: the CLI is invoked from arbitrary panes and shells
that never carry plugin env, so every invocation must resolve to the same
ledger.

## Known issues

- **Herdr agent detection vs. bundled CLIs.** Some providers ship as one
  bundled binary (gemini, qwen) that Herdr's pane agent detection does not
  yet identify as an agent. Consequence: `radio handles` can show such a
  handle as `gone` while its pane is alive, and delivery falls back from
  `herdr agent prompt` to plain `send-text`. Radio still works; the status
  column is the casualty. This is a Herdr-side detection gap, to be fixed
  there.

## License

[MIT](LICENSE)
