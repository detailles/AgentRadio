# Changelog

User-visible changes, based on the repository's version commits and tags. Unreleased 0.3.1 and 0.7.0 changes are included under 0.4.0 and 0.7.1, respectively.

## Unreleased

- Removed the standalone demo bot and its README instructions; the quick start uses real agent panes.

## 0.7.1

- Added read-only `radio tools context` snapshots for Codex agents, with table and JSON output and workspace or frequency scoping.
- Made `radio tools usage` show only authenticated local accounts, including separate named Codex and Kimi logins. Added a separate forecast line when a community-wide Codex reset is announced.
- Made `radio tools calm` work in the Windows console.
- Fixed context readings from forked Codex rollouts, named-account usage settings, and stale quota values after switching logins within the cache window.

## 0.6.1

- Added the `radio tools calm` terminal animation.
- Fixed failed quota-read caching and malformed cache handling; tightened Kimi credential and endpoint handling.
- Improved relay delivery around shell metacharacters, startup and timed-out prompts. Fixed the view with a missing ledger or non-UTF-8 output, and made the Windows CLI shim require Python 3.10 or newer.

## 0.6.0

- Added `radio tools usage` for Codex, Claude and Kimi account quotas, with ticker, table and JSON output. A shared ten-minute cache and lock bound provider reads across panes.

## 0.5.0

- Added opt-in named frequencies so selected handles can communicate across workspaces while keeping separate identities and message history. Existing ledgers migrate to schema 2 with a backup; see the [upgrade note](README.md#named-frequencies).

## 0.4.2

- Stopped the Windows relay from opening console windows.

## 0.4.1

- Held deliveries while a user is typing in the target pane and guarded relay handover against an incomplete successor script.
- Improved Codex prompt detection and Herdr UTF-8 decoding.

## 0.4.0

- Added `radio restore` and named provider accounts, including account moves that keep the handle identity.
- Moved relay and view runtime files outside the managed plugin directory so Windows plugin updates can replace it.

## 0.3.0

- Scoped handles, messages and delivery to Herdr workspaces and added handle roles.
- Added `radio repair` for ledger diagnosis and backed-up reset.

## 0.2.10

- Added the Windows CLI shim directory to the user's PATH during installation.

## 0.2.9

- Made the Windows CLI shim resolve the installed plugin path after updates.

## 0.2.8

- Added Windows support for the CLI, relay and view.

## 0.2.7

- Made the view roster discover panes across workspaces.

## 0.2.6

- Let installation succeed when optional view dependencies cannot be installed; the view can be set up later.

## 0.2.5

- Added a responsive roster sidebar to the view.

## 0.2.4

- Kept the relay running after an individual tick fails, added retry backoff and fresh-first delivery ordering, and guarded concurrent delivery acknowledgements.

## 0.2.3

- Made handle resolution case-insensitive and compacted join catch-up to the newest reply-required message per sender.

## 0.2.2

- Verified pane identity before push, kept unconfirmed deliveries for pull, and hardened message insertion against shell substitution.

## 0.2.1

- Fixed log rendering against a removed ledger column.

## 0.1.0

- Introduced the PM-only local Radio bus, push relay and read-only terminal view.
