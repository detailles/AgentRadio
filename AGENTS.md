# AgentRadio — Agent Guide

Rules for this repository. Explanations live in `README.md`; this file only
forbids, requires and routes.

## Versioning

`VERSION` in `bin/radio`, `version` in `herdr-plugin.toml` and the README badge
always move together in one commit.

Pick the bump from the change, never by habit:

- **MINOR (`0.x.0`)** — a new command or flag, new user-visible behavior
  (delivery semantics included), or a new ledger schema. A schema change also
  bumps `SCHEMA_VERSION`, and the README gains an upgrade note.
- **PATCH (`0.x.y`)** — a fix that restores intended behavior and adds none;
  the previous contract still holds.
- **MAJOR (`1.0.0` and later)** — a breaking change to the command, flag or
  ledger contract. Before 1.0 a breaking change ships as a MINOR and is named
  in the README upgrade notes.

Tag every released version `vX.Y.Z` on the commit that sets it, and push the
tag. A tag is never moved; the only exception is an approved history rewrite.
