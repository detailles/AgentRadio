# herdr-radio

Local message bus for agents running in [Herdr](https://herdr.dev) panes.
A Herdr plugin: one SQLite ledger, one stdlib-only Python CLI, one relay
daemon. No project-specific dependencies.

## Concepts

- **handle** — a name bound to a Herdr pane (or `manual` outside Herdr)
- **relay** — daemon that pushes pending deliveries into live panes

Radio is deliberately PM-only: direct messages between handles. No rooms or
broadcast — a room message is N pushed turns for N members, and most of them
are irrelevant to the recipient. Token-sensitive by design.

## Install

```bash
herdr plugin link /path/to/herdr-radio   # local development
herdr plugin install owner/repo          # from GitHub, once published
```

Put the CLI on PATH (or symlink it):

```bash
ln -s /path/to/herdr-radio/bin/radio ~/.local/bin/radio
```

Open the view by running one word in any pane you like — the TUI adopts and
labels that pane itself:

```bash
radio
```

No "open a panel" command to remember. (A keybinding-friendly plugin action
also exists for those who want one: `radio.open-view`.)

The relay daemon is separate: it starts automatically via the plugin's
startup hook (and survives restarts detached), so the view is read-only and
can be opened/closed freely. `radio relay` runs a foreground relay manually.

## Usage

Radio is provider-agnostic: it knows panes and handles, not agent kinds.
The flow is: open a pane wherever you want it, join from inside it, then run
whatever you like in that pane — an agent, a bot script, a plain shell.

```bash
radio join bot-c                 # inside the pane; binds it, labels it bot-c
radio join                       # same, adopting the pane's existing label
radio pm bot2 'hello' --from bot-c
radio pm bot2 'rapor hazir' --ref /tmp/rapor.md --from bot-c   # payload dosyada
radio handles
radio inbox bot2                 # pull path, for pane-less handles
radio log
radio part bot-c
```

If the pane already runs an agent, the agent can join itself: its shell tool
inherits `HERDR_PANE_ID`, so `radio join <handle>` works from inside the
agent session.

**The pane label IS the handle.** Joining renames the pane to the handle so
the two never drift; omitting the handle adopts the existing label. This is
what survives Herdr session restore and what the UI shows. Joining also
records the pane's agent session id (reported by Herdr's agent integrations)
into the handle row, so a restored agent can be matched back to its handle.

Sender resolution: `--from`, else `$RADIO_HANDLE`, else the handle bound to
the current pane.

## Named agent sessions (optional, provider-side)

Some providers support named sessions, and naming the session after the
handle lets provider-level restore find it (e.g. `claude --name <handle>`).
That is a provider habit, not a Radio requirement: Radio never looks at
session names, only at pane bindings. Provider-assigned session ids and
auto-derived names change across restarts; the handle does not.

## State

The ledger lives at `$RADIO_HOME/radio.db` when `RADIO_HOME` is set, otherwise
`~/.local/share/herdr-radio/radio.db`. It is deliberately one well-known path
per machine, not the Herdr plugin state dir: the CLI is invoked from arbitrary
panes and shells that never carry plugin env, so every invocation must resolve
to the same ledger.

Delivery to a pane uses `herdr agent prompt`, falling back to
`pane send-text` + `pane send-keys enter` for plain shells. Handles without
a live pane are marked `pull` and read via `radio inbox`.

## Demo

Two auto-reply bots in two panes you open yourself:

```bash
# in pane 1:  python3 /path/to/herdr-radio/demo/bot.py bot1
# in pane 2:  python3 /path/to/herdr-radio/demo/bot.py bot2
radio pm bot2 'merhaba' --from bot1   # bot2 acks automatically
```

The bots join themselves on startup; no pane plumbing needed.
