#!/usr/bin/env python3
"""Herdr plugin event hook for workspace.created (bin/workspace-created.py).

Every new workspace gets its own Radio view pane, scoped to that project. The
event payload arrives as HERDR_PLUGIN_EVENT_JSON; the workspace id is read
defensively because the event shape is Herdr's to change, with
HERDR_WORKSPACE_ID as the fallback. Any failure exits 0: an event hook must
never wedge Herdr, and a missing view pane is not an error.
"""

import json
import os
import shutil
import subprocess
import sys


def event_workspace(payload: str) -> str:
    """The workspace the event is about: the nested `workspace.workspace_id`
    when present, else a top-level `workspace_id`, else the first
    workspace_id anywhere in the payload — the event shape is Herdr's to
    change, and guessing wrong would open the view in the wrong project."""
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return ""
    if isinstance(data, dict):
        workspace = data.get("workspace")
        if isinstance(workspace, dict) and isinstance(workspace.get("workspace_id"), str):
            return workspace["workspace_id"]
        if isinstance(data.get("workspace_id"), str):
            return data["workspace_id"]
    found = ""

    def walk(node) -> None:
        """Depth-first search for the first workspace_id in a payload whose
        exact shape is not guaranteed; sets the outer `found` and stops."""
        nonlocal found
        if found:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "workspace_id" and isinstance(value, str) and value:
                    found = value
                    return
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


def main() -> int:
    """Open a Radio view pane in the new workspace; never raises."""
    workspace = event_workspace(os.environ.get("HERDR_PLUGIN_EVENT_JSON", ""))
    workspace = workspace or os.environ.get("HERDR_WORKSPACE_ID", "")
    if not workspace:
        return 0
    herdr = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr") or "herdr"
    entrypoint = "view-win" if os.name == "nt" else "view"
    try:
        subprocess.run(
            [
                herdr, "plugin", "pane", "open",
                "--plugin", "radio",
                "--entrypoint", entrypoint,
                "--workspace", workspace,
                "--no-focus",
            ],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
