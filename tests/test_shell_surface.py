"""The POSIX shell surface: the CLI link, the build hook and the briefing
hooks.

These scripts run exactly as Herdr runs them, but with HOME, RADIO_HOME, PATH
and the provider homes pointed at a temp dir, so no real user config, PATH,
ledger or plugin copy is touched. Skipped on Windows: the scripts are POSIX
shell and Windows uses its own entrypoints.

Why this file exists: setup.sh runs as the plugin build hook and can destroy
user configuration if it regresses, link-cli.sh must never overwrite an
unrelated binary, and the briefing hooks are the only channel gemini and kimi
use to receive the opening briefing — none of them had coverage before.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
POSIX_ONLY = unittest.skipIf(os.name == "nt", "POSIX shell scripts")


def fake_tools(directory: Path) -> Path:
    """A PATH directory with fake `kimi`/`gemini` markers and a `python3` that
    fails `-m venv` (no venv or PyPI access in a test) but runs the real
    interpreter for everything else, including the settings merge heredoc."""
    directory.mkdir(parents=True, exist_ok=True)
    for tool in ("kimi", "gemini"):
        marker = directory / tool
        marker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        marker.chmod(0o755)
    wrapper = directory / "python3"
    wrapper.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then exit 1; fi\n'
        f'exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return directory


@POSIX_ONLY
class LinkCliTest(unittest.TestCase):
    """bin/link-cli.sh: the `radio` link is created and repaired, and an
    unrelated file or symlink in the target directory is never touched."""

    def setUp(self):
        """A temp HOME whose ~/.local/bin the script may write to."""
        self.tmp = tempfile.TemporaryDirectory(prefix="radio-link-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.link_dir = self.home / ".local" / "bin"
        self.link = self.link_dir / "radio"

    def run_link(self, root: Path | None = None) -> subprocess.CompletedProcess:
        """Run link-cli.sh with a controlled HOME and plugin root."""
        env = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": "/bin:/usr/bin",
            "HERDR_PLUGIN_ROOT": str(root or REPO),
        }
        return subprocess.run(
            ["sh", str(REPO / "bin" / "link-cli.sh")],
            env=env, capture_output=True, text=True, cwd=str(REPO),
        )

    def test_link_is_created_and_repaired(self):
        """A missing link is created; a link into an older managed copy is repaired."""
        self.link_dir.mkdir(parents=True)
        self.assertEqual(self.run_link().returncode, 0)
        self.assertEqual(os.readlink(self.link), str(REPO / "bin" / "radio"))
        self.link.unlink()
        self.link.symlink_to("/home/x/.local/share/herdr/plugins/radio-old/bin/radio")
        self.run_link()
        self.assertEqual(os.readlink(self.link), str(REPO / "bin" / "radio"))

    def test_foreign_files_are_never_touched(self):
        """An unrelated binary, or a link outside a plugin dir, stays as it is."""
        self.link_dir.mkdir(parents=True)
        self.link.write_text("#!/bin/sh\necho not radio\n", encoding="utf-8")
        self.run_link()
        self.assertEqual(self.link.read_text(encoding="utf-8"), "#!/bin/sh\necho not radio\n")
        self.link.unlink()
        self.link.symlink_to("/usr/local/bin/something-else")
        self.run_link()
        self.assertEqual(os.readlink(self.link), "/usr/local/bin/something-else")

    def test_missing_link_dir_is_a_noop(self):
        """Without ~/.local/bin the script creates nothing."""
        self.assertEqual(self.run_link().returncode, 0)
        self.assertFalse(self.link_dir.exists())


@POSIX_ONLY
class SetupShTest(unittest.TestCase):
    """bin/setup.sh is the plugin build hook: it must never clobber user
    config and must always exit 0, whatever fails."""

    def setUp(self):
        """A temp HOME/RADIO_HOME with fake provider CLIs and a failing venv."""
        self.tmp = tempfile.TemporaryDirectory(prefix="radio-setup-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.state = Path(self.tmp.name) / "state"
        self.tools = fake_tools(Path(self.tmp.name) / "tools")
        self.home.mkdir()
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "RADIO_HOME": str(self.state),
            # Only the fake bin plus the POSIX tools: an installed uv must not
            # turn this test into a real venv build with PyPI access.
            "PATH": f"{self.tools}:/bin:/usr/bin",
            "KIMI_CODE_HOME": str(self.home / ".kimi-code"),
            "GEMINI_CLI_HOME": str(self.home / ".gemini"),
        }

    def run_setup(self, root: Path | None = None) -> subprocess.CompletedProcess:
        """Run the build hook from the given plugin root (default: this repo)."""
        return subprocess.run(
            ["sh", str((root or REPO) / "bin" / "setup.sh")],
            env=self.env, capture_output=True, text=True, cwd=str(root or REPO),
        )

    def test_malformed_gemini_settings_are_left_untouched(self):
        """A settings.json that cannot be parsed is reported, not rewritten."""
        settings = self.home / ".gemini" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text("{not json at all", encoding="utf-8")
        settings.chmod(0o640)
        result = self.run_setup()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(settings.read_text(encoding="utf-8"), "{not json at all")
        self.assertEqual(settings.stat().st_mode & 0o777, 0o640)
        self.assertIn("leaving", result.stderr)

    def test_valid_settings_keep_their_content_and_mode(self):
        """A user's settings gain the hook, keep their keys and their mode; the
        kimi hook lands in its own home; the failed venv still exits 0."""
        settings = self.home / ".gemini" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(
            json.dumps({"theme": "dark", "hooks": {"SessionStart": []}}), encoding="utf-8"
        )
        settings.chmod(0o600)
        result = self.run_setup()
        self.assertEqual(result.returncode, 0)
        payload = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(payload["theme"], "dark")
        commands = [
            hook["command"]
            for entry in payload["hooks"]["SessionStart"]
            for hook in entry["hooks"]
        ]
        self.assertTrue(any("radio-gemini-hook.sh" in command for command in commands))
        self.assertEqual(settings.stat().st_mode & 0o777, 0o600)
        kimi = self.home / ".kimi-code" / "config.toml"
        self.assertIn("radio-kimi-hook.sh", kimi.read_text(encoding="utf-8"))
        self.assertIn("WARNING", result.stderr)  # the view deps could not build
        self.assertFalse((self.state / "venv").exists())  # no partial venv left

    def test_managed_copy_drops_a_legacy_venv_and_a_clone_keeps_its_own(self):
        """Only a managed plugin copy removes a pre-0.3.1 .venv; a dev clone's
        venv is reported and kept."""
        clone = Path(self.tmp.name) / "clone"
        shutil.copytree(REPO / "bin", clone / "bin")
        (clone / ".venv").mkdir()
        result = self.run_setup(clone)
        self.assertEqual(result.returncode, 0)
        self.assertIn("development venv", result.stderr)
        self.assertTrue((clone / ".venv").exists())
        managed = Path(self.tmp.name) / "herdr" / "plugins" / "radio-abc"
        shutil.copytree(REPO / "bin", managed / "bin")
        (managed / ".venv").mkdir()
        result = self.run_setup(managed)
        self.assertEqual(result.returncode, 0)
        self.assertFalse((managed / ".venv").exists())


@POSIX_ONLY
class BriefingHooksTest(unittest.TestCase):
    """The gemini and kimi briefing hooks: a no-op outside radio sessions, one
    briefing per kimi session, and the provider's documented JSON on stdout."""

    def setUp(self):
        """A temp RADIO_HOME and a python3 the hooks can call."""
        self.tmp = tempfile.TemporaryDirectory(prefix="radio-hooks-")
        self.addCleanup(self.tmp.cleanup)
        self.tools = fake_tools(Path(self.tmp.name) / "tools")

    def hook(self, name: str, env: dict | None = None,
             stdin: str = "") -> subprocess.CompletedProcess:
        """Run one hook with a controlled environment (no ambient RADIO_HANDLE)."""
        base = {
            **os.environ,
            "PATH": f"{self.tools}:{os.environ.get('PATH', '')}",
            "RADIO_HOME": str(Path(self.tmp.name) / "state"),
        }
        base.pop("RADIO_HANDLE", None)
        base.update(env or {})
        return subprocess.run(
            ["bash", str(REPO / "bin" / name)],
            input=stdin, env=base, capture_output=True, text=True, cwd=str(REPO),
        )

    def test_gemini_hook_is_empty_outside_a_radio_session(self):
        """Without RADIO_HANDLE the hook emits empty JSON and exits 0."""
        result = self.hook("radio-gemini-hook.sh")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {})

    def test_gemini_hook_injects_the_briefing(self):
        """With RADIO_HANDLE the briefing rides SessionStart additionalContext."""
        result = self.hook("radio-gemini-hook.sh", {"RADIO_HANDLE": "bob"})
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(payload["hookEventName"], "SessionStart")
        self.assertIn("bob", payload["additionalContext"])

    def test_kimi_hook_noops_without_a_session(self):
        """No handle, no payload, or no session id: no output, exit 0."""
        self.assertEqual(self.hook("radio-kimi-hook.sh").stdout, "")
        for payload in (
            {"hook_event_name": "UserPromptSubmit", "session_id": ""},
            {"hook_event_name": "SessionStart", "session_id": "s1"},
            {},
        ):
            result = self.hook(
                "radio-kimi-hook.sh", {"RADIO_HANDLE": "bob"}, stdin=json.dumps(payload)
            )
            self.assertEqual(result.stdout, "")

    def test_kimi_hook_briefs_once_per_session(self):
        """The first prompt of a session gets the briefing; the next one does not."""
        payload = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s1"})
        first = self.hook("radio-kimi-hook.sh", {"RADIO_HANDLE": "bob"}, stdin=payload)
        self.assertEqual(first.returncode, 0)
        self.assertIn("bob", first.stdout)
        second = self.hook("radio-kimi-hook.sh", {"RADIO_HANDLE": "bob"}, stdin=payload)
        self.assertEqual(second.stdout, "")
        # A different session is briefed again.
        other = self.hook(
            "radio-kimi-hook.sh", {"RADIO_HANDLE": "bob"},
            stdin=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s2"}),
        )
        self.assertIn("bob", other.stdout)
