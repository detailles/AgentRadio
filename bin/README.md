# herdr-radio

Local message bus for agents running in [Herdr](https://herdr.dev) panes.
A Herdr plugin: one SQLite ledger, one stdlib-only Python CLI, one relay
daemon. No project-specific dependencies.

## Concepts

- **handle** — a name bound to a Herdr pane (or `manual` outside Herdr)
- **room** — a named group; `say` reaches every member except the sender
- **relay** — daemon that pushes pending deliveries into live panes

## Install

```bash
herdr plugin link /path/to/herdr-radio   # local development
herdr plugin install owner/repo          # from GitHub, once published
```

Put the CLI on PATH (or symlink it):

```bash
ln -s /path/to/herdr-radio/bin/radio ~/.local/bin/radio
```

Open the relay pane (delivery + live log):

```bash
herdr plugin pane open --plugin radio --entrypoint relay
```

## Usage

```bash
radio join bot1 --room runners   # run inside the pane; binds HERDR_PANE_ID
radio pm bot2 'hello' --from bot1
radio say runners 'status?' --from bot1
radio handles
radio rooms
radio inbox bot2                 # pull path, for pane-less handles
radio log
radio part bot1
```

Sender resolution: `--from`, else `$RADIO_HANDLE`, else the handle bound to
the current pane.

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

Two auto-reply bots in two panes:

```bash
herdr workspace create --label radio-demo
herdr pane run <w:p1> python3 /path/to/herdr-radio/demo/bot.py bot1
herdr pane run <w:p2> python3 /path/to/herdr-radio/demo/bot.py bot2
radio pm bot2 'merhaba' --from bot1   # bot2 acks automatically
```
