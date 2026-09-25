<p align="center">
  <img src="docs/icon.png" alt="AgentRadio icon" width="96">
</p>
<h2 align="center">AgentRadio</h2>
<p align="center">
  A local message bus for agents running in <a href="https://herdr.dev">Herdr</a> panes.<br>
  Agents join by name, talk in direct messages, and get every reply pushed straight into their pane.
</p>
<p align="center">
  <img src="https://img.shields.io/badge/version-0.4.0-7dcfff?style=flat-square" alt="version">
  <img src="https://img.shields.io/badge/python-3.10%2B%20stdlib-bb9af7?style=flat-square" alt="python">
  <img src="https://img.shields.io/badge/license-MIT-9ece6a?style=flat-square" alt="license">
  <img src="https://img.shields.io/badge/platform-macos%20%7C%20linux%20%7C%20windows-e0af68?style=flat-square" alt="platform">
</p>
<p align="center">
  <a href="#install">Install</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#commands">Commands</a> ·
  <a href="#providers">Providers</a> ·
  <a href="#design-decisions">Design decisions</a>
</p>

<p align="center">
  <img src="docs/demo.gif" alt="Demo: three agents on claude, codex and gemini join by name, hand off work by file reference, and hold a back-and-forth over radio while the view logs every message">
</p>

One SQLite ledger, one stdlib-only Python CLI, one relay daemon. No servers, no dependencies, no accounts.

PM-only and token-sensitive by design: no rooms, no broadcast, no chatter. A message costs exactly one delivered envelope.

## Highlights

- **Handles, not plumbing.** A handle is a name bound to a pane, and the pane label *is* the handle.
- **Scoped to the project.** Every Herdr workspace is its own bus — its own roster, its own names — so the same handle can live in several projects without collisions.
- **Roles.** A handle carries a role paragraph that rides its briefing, editable any time with `radio role`.
- **Push delivery.** The relay drops envelopes into live panes, so agents never poll.
- **Real dialogue.** `--reply-required` marks the envelope `reply=required`. The recipient answers over radio, and that answer is pushed back the same way. Agents ask, answer, push back and agree without a human relaying.
- **Busy-aware.** The relay holds a delivery while the target agent is mid-turn, stuck on a dialog or still booting, then pushes it when the agent can take input.
- **Briefing on join.** Agents learn the protocol through a provider-native system channel, not a wasted first turn.
- **Payloads by reference.** `--ref /path/to/file` sends a pointer, not pasted text.
- **Any mix of agents.** claude, codex, gemini, kimi, opencode and more share one bus. See [Providers](#providers).

## How it works

```mermaid
flowchart LR
  A["pane: planner<br/>radio pm coder …"] -- write --> L[("radio.db<br/>SQLite ledger")]
  L -- pending --> R["relay"]
  R -- "herdr agent prompt" --> B["pane: coder"]
  V["radio view"] -. read-only .-> L
```

`radio pm` writes the message and a pending delivery to the ledger. The relay picks it up, checks that the target pane is live and ready, and submits the envelope into it. The view only reads the ledger, so you can open and close it at any time.

## Install

Requires **herdr ≥ 0.9.0** and **Python 3.10+**, nothing else. The CLI and relay are pure stdlib, and the view's one dependency (Textual) is installed automatically into a venv under the plugin state dir (`~/.local/share/herdr-radio/venv`) — outside the managed plugin dir, so an open view pane never blocks an update.

```bash
herdr plugin install detailles/AgentRadio   # or: herdr plugin link /path/to/clone
```

The relay starts itself via the plugin's startup hook. If the view's venv step is skipped during install (no PyPI access, no pip/uv), the install still succeeds: CLI and relay work, and the view activates later with `sh bin/setup.sh` (Windows: `bin\setup.cmd`).

To update an installed plugin, run the same install command again. Herdr replaces the managed copy in place and the startup hook refreshes the CLI link or shim; no uninstall is needed.

### macOS and Linux

The install links `radio` into `~/.local/bin` when that directory exists, and the startup hook repairs that link on every Herdr start, so a reinstall or a moved plugin directory never leaves it dangling. If `~/.local/bin` is not on your PATH, or you want the link elsewhere, point one at the plugin root yourself:

```bash
ROOT="$(herdr plugin list --json | python3 -c 'import json,sys; print(next(p["plugin_root"] for p in json.load(sys.stdin)["result"]["plugins"] if p["plugin_id"]=="radio"))')"
ln -s "$ROOT/bin/radio" ~/.local/bin/radio
```

### Windows (preview)

The install writes a stable `radio.cmd` shim into `%USERPROFILE%\.local\bin` — it resolves the plugin root at run time — and adds the shim directory to your user PATH automatically. Restart the terminal (and Herdr) once so already-open panes see the new PATH. `herdr plugin install` needs `git` on PATH (for example `winget install Git.Git`).

Herdr's plugin support is preview on Windows: CLI, push delivery and the view work; the gemini/kimi briefing hooks are POSIX-only and are not installed there.

## Quick start

Open two panes and join each one. `radio join` asks which agent CLI to launch into the pane (claude, codex, gemini, …), or pick `none` to just register the handle:

```bash
radio join planner    # pane 1
radio join coder      # pane 2
```

planner hands off a task and asks for an answer:

```bash
radio pm coder 'Fix the flaky retry test. Plan in the ref.' --ref /tmp/retry-plan.md --reply-required
```

coder's pane lights up:

```
[RADIO_MESSAGE id=1 kind=pm from=planner to=coder reply=required]
Radio PM from planner
Reply required: answer this over radio.
Fix the flaky retry test. Plan in the ref.
Ref: /tmp/retry-plan.md
[END_RADIO_MESSAGE id=1]
```

coder answers from its own pane, and the reply is pushed back into planner's pane. Neither side polls:

```bash
radio pm planner 'Root cause is a real sleep. Fix it or quarantine?' --reply-required
```

Inside a joined pane the sender is resolved automatically. From anywhere else, add `--from <handle>`.

No agent CLI? Try the demo bots. Each one prints what it receives and acks back once:

```bash
python3 <plugin_root>/demo/bot.py planner   # pane 1
python3 <plugin_root>/demo/bot.py coder     # pane 2
radio pm coder 'ping' --from planner
```

## Commands

| Command | What it does |
|---|---|
| `radio join <handle>` | Bind this pane to a handle; launches an agent if you pick one |
| `radio pm <h> 'msg'` | Direct message in this project. `--ref <file>` sends a file reference, `--reply-required` asks for an answer |
| `radio handles` | This project's roster: live, gone, or pull. Outside Herdr: every project, grouped |
| `radio role <h> 'text'` | Set, print, or `--clear` a handle's role paragraph; it rides the next briefing |
| `radio account <add\|list\|remove\|move>` | Named provider accounts: a config home plus launch environment per login; `move` carries a session to another account |
| `radio inbox` | Index of your messages: ids, senders, status. Nothing consumed |
| `radio show <id>` | Read one exact body; records the delivery |
| `radio log` | This project's message log |
| `radio part <h>` | Remove a handle |
| `radio restore <h>` | Bring a handle's agent back into its pane (resumes its recorded session) |
| `radio repair` | Ledger health report; `--reset` snapshots it and starts empty |
| `radio` | Open the dashboard in the current pane |

Sender resolution: `--from`, else `$RADIO_HANDLE`, else the handle bound to the current pane.

## The view

Run one word in any pane, and the dashboard adopts and labels it:

```bash
radio
```

It shows the live message stream (`⚠reply` marks reply-required messages), a roster with each handle's pane and live state (idle, working, pull, or a missing/reused pane), and pending/failed/delivered counts. It starts scoped to its workspace; `a` widens it to every workspace, where the roster groups by project. `h` toggles the roster; below 80 columns it folds into the header bar. The relay is separate, so the view is read-only and can be opened and closed freely.

## Handles, projects and panes

**The pane label IS the handle.** Joining renames the pane to the handle so the two never drift, and this is what survives Herdr session restore. The pane's agent session id is recorded too, so a restored agent is matched back to its handle, and rejoining a handle offers to bring its recorded session back.

**A handle belongs to its project.** Every Herdr workspace is its own scope: joining from a pane records that workspace, and inside it every command resolves there — `radio handles` lists this project's agents, `radio pm` reaches this project, `radio log` shows this project's traffic. The same name can join in several projects, and a miss says so plainly (`no handle "x" in this workspace`) instead of reaching across projects. Handles joined outside Herdr (or before scoping) live in one global namespace reachable from every project; scripts can address a scoped handle explicitly as `w1:alice`, the internal form that rosters and envelopes never show.

**Roles.** `radio role <handle> "…"` stores a role paragraph on the handle, `radio role <handle>` prints it, `--clear` removes it. The paragraph rides the briefing, so the agent learns its role on its next join or resume; a role change is never pushed as a message.

**New projects come with a view.** When Herdr creates a workspace, a `workspace.created` hook opens a scoped Radio view pane in it, so every project starts with its own dashboard.

**Bring an agent back.** `radio restore <handle>` re-launches the handle's recorded provider, account and session in its own pane (the briefing carries the workspace and role again); without a recorded session it starts fresh. It never creates layout: if the pane is gone, it says which workspace to open a pane in.

**Accounts.** Any number of logins per provider can share the bus. `radio account add work --provider codex --home ~/.codex-work` registers a config home — the default follows the provider's convention (`codex2` → `~/.codex-account-2`) — and `--env NAME=VALUE` (repeatable) adds launch-time environment for unusual setups. `radio join coder --account work` launches that login and records it on the handle, so `radio restore coder` brings the same login back without repeating the selector. The account is a property, never part of the name: the roster shows it as a separate `account:` field. To move a conversation to another login, `radio account move coder --to personal` copies the session files into the target home (codex, claude, kimi and pi) and switches the handle over; the old account keeps its data. `radio account list` shows each home, its extra environment and auth state; removing an account a handle still uses is refused.

Delivery uses `herdr agent prompt`, falling back to `send-text` for plain shells. Handles without a live pane are marked `pull`: their messages wait for `radio inbox` / `radio show <id>`. When a handle rejoins on a live pane, only the newest reply-required message per sender is pushed; the rest of the backlog stays available through `radio inbox` instead of flooding the fresh agent. The relay also holds a delivery while the target pane is focused — the user is at that pane, and a push would land in whatever they are typing. After 30 seconds the visible composer decides: unsent text keeps the hold, an empty composer (or a provider whose UI we cannot read) lets the push through, so a pane left focused never starves.

Long messages are an anti-pattern: past ~1200 characters the CLI nudges you to write the payload to a file and send `--ref` instead.

## Providers

The bus layer is provider-agnostic. `radio join` can launch an agent CLI into the pane with the briefing injected through a provider-native system channel, never as a radio message and never spending a turn.

| Provider | Briefing channel | Session resume | Status |
|---|---|---|---|
| claude | `--append-system-prompt` | `--resume <id>` | tested |
| codex | `-c developer_instructions=…` | `codex resume <id>` | tested |
| opencode | per-handle config `instructions` | `--session <id>` | tested |
| gemini | SessionStart hook | `--resume <id>` | tested |
| kimi | UserPromptSubmit hook | `--session <id>` | tested |
| qwen | `--append-system-prompt` | `--resume <id>` | written, untested |
| pi | — | `--session <id>` | written, untested |

For gemini and qwen, join assigns the session id itself, so resume is exact. The gemini and kimi briefing hooks are installed on macOS/Linux only.

### Security notes

Radio-launched agents must answer messages unattended, so some providers launch with relaxed approval:

- **gemini / qwen** launch with `--approval-mode yolo`: every tool call in that pane is auto-approved
- **opencode** joins write a per-handle config allowing `--ref` paths outside the working directory
- **codex** launches with per-process `-c` overrides; your global `config.toml` is untouched

These apply only to panes started by `radio join`. Do not point a yolo-mode handle at work you would not auto-approve.

## Design decisions

AgentRadio is deliberately small. Every mechanism here earns its place by per-token cost, because every briefing and every delivered message lands in an agent's context window.

Deliberately excluded:

- **Rooms / broadcast.** A room message reaches mostly the wrong agents, and every one of them pays tokens for it. DMs only; `radio handles` answers "who is here" without fan-out.
- **Project isolation, not rooms.** Scoping is identity: each Herdr workspace has its own names, roster and delivery, so the same handle can exist in several projects without collisions. It is not a fan-out mechanism, and agents cannot message across projects; scripts can address another project explicitly, a deliberately narrow escape hatch.
- **Task / handoff tracking.** A shared task board means durable coordination state (ownership, lifecycle, conflict semantics), which roughly doubles the complexity of the bus. It may come later on the roadmap, introduced deliberately rather than absorbed by default. Until then, handoffs travel as refs: point to a file, keep the message short.
- **MCP transport.** A tool schema costs context in every session permanently; a CLI costs it only when used. Agents already have a shell.
- **Non-herdr agents (plain tmux, SSH).** The bus is a herdr plugin and presence is pane-derived; agents outside herdr are out of scope by design.
- **Multi-machine federation.** One bus per herdr instance. Linking instances is a separate, later question.

## State

The ledger lives at `$RADIO_HOME/radio.db`, or `~/.local/share/herdr-radio/radio.db` by default. One well-known path per machine: the CLI is invoked from arbitrary panes, so every invocation must resolve to the same ledger.

The ledger records a schema version. A ledger written by a newer radio is refused with a clear message instead of failing halfway; upgrade the plugin, or run `radio repair --reset`, which snapshots the ledger beside itself and starts empty. `radio repair` alone prints a health report (schema, integrity, counts, relay state) and changes nothing. Upgrades are one-way: an older radio cannot use a ledger a newer one has upgraded.

## Known issues

- **Herdr agent detection vs. bundled CLIs.** Providers that ship as one bundled binary (gemini, qwen) aren't yet recognized as agents by Herdr's pane detection. Consequence: such a handle can show as `gone` while its pane is alive, and delivery falls back to `send-text`. Radio still works; the status column is the casualty. Herdr-side gap, to be fixed there.
- **Windows (preview).** Herdr's plugin surface is preview on Windows: Radio's CLI, relay, and view work there, but the gemini/kimi briefing hooks are POSIX-only, and a radio-launched agent that ships as a `.cmd` shim starts through `cmd /c`.
- **Windows updates on 0.3.0 and older.** The relay kept its working directory inside the managed plugin dir, so `herdr plugin install` failed with a file-in-use error while the relay was running. 0.4.0 moves the relay out; until you update, stop the relay once (`Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*bin\radio*relay*" } | Stop-Process -Force`) and reinstall.

## License

[MIT](LICENSE)
