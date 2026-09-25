#!/usr/bin/env python3
"""Tests for bin/radio: ledger semantics, envelope format, launch argv matrix,
and relay delivery outcomes. herdr and pane I/O are monkeypatched — no test
spawns a process, execs, or touches the real ~/.local/share/herdr-radio."""

import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Must be set before import: bin/radio resolves STATE_DIR at module load.
os.environ["RADIO_HOME"] = tempfile.mkdtemp(prefix="radio-test-home-")

# spec_from_file_location alone returns None for an extensionless file, so the
# source loader is passed explicitly.
_loader = importlib.machinery.SourceFileLoader("radio", str(REPO / "bin" / "radio"))
spec = importlib.util.spec_from_file_location("radio", REPO / "bin" / "radio", loader=_loader)
radio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(radio)


class RadioTestCase(unittest.TestCase):
    """Shared fixture: a hermetic temp ledger plus helpers that seed handles, messages and
    deliveries."""

    def setUp(self):
        """Point radio's module paths at a fresh temp ledger and strip ambient herdr/pane env."""
        self._saved = (radio.STATE_DIR, radio.DB_PATH, radio.LOCK_PATH)
        # Hermetic identity: an ambient Herdr pane env (running the suite from
        # inside a pane) must not leak into current_session_ref/cmd_join.
        self._saved_env = {k: os.environ.pop(k, None)
                           for k in ("HERDR_ENV", "HERDR_PANE_ID", "RADIO_HANDLE")}
        tmp = Path(tempfile.mkdtemp(prefix="radio-test-"))
        radio.STATE_DIR = tmp
        radio.DB_PATH = tmp / "radio.db"
        radio.LOCK_PATH = tmp / "relay.lock"
        self.conn = radio.connect()

    def tearDown(self):
        """Close the ledger and restore the module paths and environment the test found."""
        self.conn.close()
        radio.STATE_DIR, radio.DB_PATH, radio.LOCK_PATH = self._saved
        for key, value in self._saved_env.items():
            if value is not None:
                os.environ[key] = value

    def add_handle(self, name, ref="manual", agent=None, agent_session=None,
                   last_seen=None, workspace="", role=None, account=None):
        """Insert one handle row directly, defaulting the columns a test does not care about."""
        ts = last_seen or radio.now()
        self.conn.execute(
            "INSERT INTO handles(workspace, name, session_ref, agent, agent_session, role, account, created_at, last_seen) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (workspace, name, ref, agent, agent_session, role, account, ts, ts),
        )
        self.conn.commit()

    def pm(self, sender, to, text, ref=None, reply_required=False,
           from_ws="", to_ws=""):
        """Record a PM and enqueue its delivery, as cmd_pm would; returns the message id."""
        mid = radio.record_message(self.conn, "pm", sender, text,
                                   to_handle=to, ref=ref, reply_required=reply_required,
                                   from_ws=from_ws, to_ws=to_ws)
        radio.enqueue_delivery(self.conn, mid, to, to_ws)
        self.conn.commit()
        return mid

    def message_row(self, mid):
        """Fetch the messages row for an id."""
        return self.conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()

    def delivery_row(self, mid):
        """Fetch the single deliveries row for a message id."""
        return self.conn.execute(
            "SELECT * FROM deliveries WHERE message_id = ?", (mid,)
        ).fetchone()

    def backdate_attempt(self, mid, seconds):
        """Age a delivery's last attempt past RETRY_AFTER_S so the next tick selects it."""
        old = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(
            timespec="milliseconds"
        )
        self.conn.execute("UPDATE deliveries SET last_attempt_at=? WHERE message_id=?",
                          (old, mid))
        self.conn.commit()


class PaneIdOfTest(RadioTestCase):
    """pane_id_of: a herdr session ref carries the pane id; manual refs have none."""

    def test_herdr_ref_vs_manual(self):
        """herdr refs yield their pane id, manual refs yield None."""
        self.assertEqual(radio.pane_id_of("herdr:w1:p1"), "w1:p1")
        self.assertIsNone(radio.pane_id_of("manual"))


class FormatForPaneTest(RadioTestCase):
    """The pane envelope: header fields plus the reply and ref lines."""

    def test_envelope_variants(self):
        """Plain, reply-required and ref messages each produce the expected envelope lines."""
        mid = self.pm("alice", "bob", "hello")
        out = radio.format_for_pane(self.message_row(mid))
        self.assertIn(f"id={mid}", out)
        self.assertIn("from=alice", out)
        self.assertIn("to=bob", out)
        self.assertIn("reply=not-required", out)
        self.assertNotIn("Reply required", out)
        self.assertNotIn("Ref:", out)

        mid = self.pm("alice", "bob", "answer me", reply_required=True)
        out = radio.format_for_pane(self.message_row(mid))
        self.assertIn("reply=required", out)
        self.assertIn("Reply required: answer this over radio.", out)

        mid = self.pm("alice", "bob", "see file", ref="/tmp/report.md")
        out = radio.format_for_pane(self.message_row(mid))
        self.assertIn("\nRef: /tmp/report.md", out)


class BriefingTextTest(RadioTestCase):
    """The opening briefing an agent receives on join."""

    def test_handle_and_no_polling_rule(self):
        """The briefing names the handle and carries the no-polling rule."""
        text = radio.briefing_text("scout")
        self.assertIn('You are on Radio as "scout"', text)
        self.assertIn("NEVER poll `radio inbox`", text)


class LaunchArgvTest(RadioTestCase):
    """launch_argv: per-provider session flags, the briefing injection, and unknown providers."""

    def test_provider_matrix(self):
        """Each provider gets its argv shape: resume vs fresh, briefing, yolo and unknown-
        provider exit."""
        argv = radio.launch_argv("claude", "h", None)
        self.assertIn("--append-system-prompt", argv)
        self.assertEqual(argv[-2:], ["--name", "h"])
        argv = radio.launch_argv("claude", "h", "sid")
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--resume") + 1], "sid")

        argv = radio.launch_argv("codex", "h", None)
        self.assertIn("tui.terminal_title=[]", argv)
        self.assertTrue(any(a.startswith("developer_instructions=") for a in argv))
        argv = radio.launch_argv("codex", "h", "sid")
        self.assertEqual(argv[-2:], ["resume", "sid"])

        argv = radio.launch_argv("kimi", "h", "sid")
        self.assertEqual(argv, ["kimi", "--session", "sid"])
        self.assertFalse(any("You are on Radio" in a for a in argv))

        argv = radio.launch_argv("qwen", "h", "uuid-1", fresh=True)
        self.assertIn("--session-id", argv)
        self.assertIn("--approval-mode", argv)
        self.assertIn("yolo", argv)
        argv = radio.launch_argv("qwen", "h", "uuid-1")
        self.assertIn("--resume", argv)
        self.assertIn("yolo", argv)

        argv = radio.launch_argv("gemini", "h", "uuid-2", fresh=True)
        self.assertIn("--session-id", argv)
        argv = radio.launch_argv("gemini", "h", "uuid-2")
        self.assertIn("--resume", argv)
        self.assertIn("yolo", argv)

        self.assertEqual(radio.launch_argv("opencode", "h", "sid"),
                         ["opencode", "--session", "sid"])
        self.assertEqual(radio.launch_argv("opencode", "h", None), ["opencode"])

        self.assertEqual(radio.launch_argv("pi", "h", None), ["pi", "--name", "h"])
        self.assertEqual(radio.launch_argv("pi", "h", "sid"),
                         ["pi", "--session", "sid", "--name", "h"])

        with self.assertRaises(SystemExit):
            radio.launch_argv("emacs", "h", None)


class EnqueueDeliveryTest(RadioTestCase):
    """enqueue_delivery: a pane-bound handle gets a push, everything else a pull."""

    def test_push_vs_pull_binding(self):
        """Pane-bound handles become pending pushes; manual refs become pull."""
        self.add_handle("onpane", ref="herdr:w1:p1")
        mid = self.pm("alice", "onpane", "hi")
        self.assertEqual(self.delivery_row(mid)["status"], "pending")

        self.add_handle("offgrid", ref="manual")
        mid = self.pm("alice", "offgrid", "hi")
        self.assertEqual(self.delivery_row(mid)["status"], "pull")


class CmdPmTest(RadioTestCase):
    """cmd_pm: target resolution, the printed confirmation, and the ref/reply hints."""

    def pm_args(self, to, text, reply_required=False):
        """Build the argparse namespace cmd_pm expects."""
        return argparse.Namespace(sender="alice", to=to, text=[text],
                                  ref=None, reply_required=reply_required)

    def run_pm(self, args):
        """Run cmd_pm with stdout captured and return the printed text."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            radio.cmd_pm(self.conn, args)
        return buf.getvalue()

    def test_unknown_target_exits(self):
        """An unknown target exits with a message."""
        with self.assertRaises(SystemExit):
            radio.cmd_pm(self.conn, self.pm_args("ghost", "hi"))

    def test_happy_path_records_and_prints(self):
        """A valid PM records a pending delivery and prints its id and target."""
        self.add_handle("bob", ref="herdr:w1:p1")
        out = self.run_pm(self.pm_args("bob", "hi"))
        self.assertIn("pm #1 -> bob", out)
        row = self.delivery_row(1)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["target"], "bob")

    def test_long_message_nudges_ref(self):
        """A very long message suggests --ref instead of inline text."""
        self.add_handle("bob")
        out = self.run_pm(self.pm_args("bob", "x" * 1300))
        self.assertIn("--ref <path>", out)

    def test_reply_required_prints_no_poll_note(self):
        """reply-required prints the do-not-poll note."""
        self.add_handle("bob")
        out = self.run_pm(self.pm_args("bob", "ping", reply_required=True))
        self.assertIn("do NOT poll `radio inbox`", out)


class PullPathTest(RadioTestCase):
    """The pull path: reading a message marks it delivered only for its recipient."""

    def test_show_marks_only_recipient_delivered(self):
        """A third party's show leaves the delivery alone; the recipient's show marks it
        delivered."""
        self.add_handle("bob", ref="manual")
        mid = self.pm("alice", "bob", "pull me")
        self.assertEqual(self.delivery_row(mid)["status"], "pull")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            radio.cmd_show(self.conn, argparse.Namespace(message_id=mid, by="alice"))
        self.assertEqual(self.delivery_row(mid)["status"], "pull")

        with contextlib.redirect_stdout(buf):
            radio.cmd_show(self.conn, argparse.Namespace(message_id=mid, by="bob"))
        row = self.delivery_row(mid)
        self.assertEqual(row["status"], "delivered")
        self.assertIsNotNone(row["delivered_at"])


class CmdPartTest(RadioTestCase):
    """cmd_part: leaving fails every outstanding delivery for the handle."""

    def test_part_fails_outstanding_deliveries(self):
        """Both pull and pending deliveries become failed when the handle parts."""
        self.add_handle("bob", ref="manual")
        self.add_handle("onpane", ref="herdr:w1:p1")
        mid_pull = self.pm("alice", "bob", "a")
        mid_pending = self.pm("alice", "onpane", "b")
        with contextlib.redirect_stdout(io.StringIO()):
            radio.cmd_part(self.conn, argparse.Namespace(handle="bob"))
            radio.cmd_part(self.conn, argparse.Namespace(handle="onpane"))
        self.assertEqual(self.delivery_row(mid_pull)["status"], "failed")
        self.assertEqual(self.delivery_row(mid_pending)["status"], "failed")


class ComposerDraftTest(RadioTestCase):
    """composer_has_draft reads the visible composer line for providers whose
    prompt marker we know; unknown providers report None (push)."""

    def setUp(self):
        """Stub herdr so composer_has_draft reads a visible-text fixture."""
        super().setUp()
        self._herdr = radio.herdr
        self.addCleanup(setattr, radio, "herdr", self._herdr)
        radio._composer_cache.clear()
        self.addCleanup(radio._composer_cache.clear)
        self.visible = ""
        radio.herdr = lambda *args, **kwargs: subprocess.CompletedProcess(
            list(args), 0, stdout=self.visible, stderr=""
        )

    def test_codex_placeholder_is_empty_and_text_is_a_draft(self):
        """The codex placeholder counts as empty; typed text counts as a draft."""
        self.visible = "»⠁Ask Codex to do anything⡀\n  gpt-6\n"
        self.assertFalse(radio.composer_has_draft("codex", "w1:p1"))
        radio._composer_cache.clear()
        self.visible = "»⠁yarım taslak⡀\n"
        self.assertTrue(radio.composer_has_draft("codex", "w1:p1"))

    def test_codex_prompt_glyph_variants_are_recognized(self):
        # The Windows VM capture renders the prompt as Γ; macOS as ».
        """The Gamma and single-angle renderings match the codex marker set like the double
        angle."""
        self.visible = "Γ yarim taslak\n"
        self.assertTrue(radio.composer_has_draft("codex", "w1:p1"))
        radio._composer_cache.clear()
        self.visible = "› Ask Codex to do anything\n"
        self.assertFalse(radio.composer_has_draft("codex", "w1:p1"))

    def test_claude_marker_only_is_empty(self):
        """An empty claude marker line is empty; text after it is a draft."""
        self.visible = "❯\n─────\n"
        self.assertFalse(radio.composer_has_draft("claude", "w1:p1"))
        radio._composer_cache.clear()
        self.visible = "❯ hello\n"
        self.assertTrue(radio.composer_has_draft("claude", "w1:p1"))

    def test_unknown_provider_or_failure_is_none(self):
        """Unknown providers and failed reads report None, which the caller treats as push."""
        self.visible = "❯ hello\n"
        self.assertIsNone(radio.composer_has_draft("kimi", "w1:p1"))
        radio.herdr = lambda *args, **kwargs: subprocess.CompletedProcess(
            list(args), 1, stdout="", stderr="x"
        )
        radio._composer_cache.clear()
        self.assertIsNone(radio.composer_has_draft("claude", "w1:p2"))

    def test_cache_avoids_a_read_per_tick(self):
        """Two calls inside the cache window cause one herdr read."""
        calls = []

        def counting(*args, **kwargs):
            """Record each stub read so the test can count them."""
            calls.append(args)
            return subprocess.CompletedProcess(list(args), 0, stdout="❯\n", stderr="")

        radio.herdr = counting
        radio.composer_has_draft("claude", "w1:p1")
        radio.composer_has_draft("claude", "w1:p1")
        self.assertEqual(len(calls), 1)


class FocusHoldTest(RadioTestCase):
    """A focused pane holds the push; after FOCUS_HOLD_S the composer decides,
    so a pane left focused does not starve."""

    def setUp(self):
        """Bind bob to a focused pane and stub the pane fetch/push/draft calls."""
        super().setUp()
        self.add_handle("bob", ref="herdr:w1:p1", agent="codex", agent_session="s1")
        self.mid = self.pm("alice", "bob", "hi")
        self._fetch = radio.fetch_pane
        self._push = radio.push_to_pane
        self._draft = radio.composer_has_draft
        self.addCleanup(self._restore)
        self.pushes = []
        radio.push_to_pane = lambda pane_id, text: (self.pushes.append(pane_id), (True, None))[1]
        radio.fetch_pane = lambda pane_id: {
            "label": "bob", "agent": "codex", "agent_status": "idle", "focused": True
        }

    def _restore(self):
        """Put the stubbed relay functions back."""
        radio.fetch_pane = self._fetch
        radio.push_to_pane = self._push
        radio.composer_has_draft = self._draft

    def delivery(self):
        """The delivery row under test."""
        return self.delivery_row(self.mid)

    def backdate_hold(self):
        """Age focus_hold_since past FOCUS_HOLD_S so the composer check applies."""
        old = (
            datetime.now(timezone.utc) - timedelta(seconds=radio.FOCUS_HOLD_S + 5)
        ).isoformat(timespec="milliseconds")
        self.conn.execute(
            "UPDATE deliveries SET focus_hold_since=? WHERE message_id=?", (old, self.mid)
        )
        self.conn.commit()

    def test_focused_pane_starts_a_hold_without_pushing(self):
        """A focused pane starts the hold: nothing is pushed and focus_hold_since is stamped."""
        radio.composer_has_draft = lambda provider, pane_id: False
        events = radio.relay_tick(self.conn)
        row = self.delivery()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(self.pushes, [])
        self.assertIsNotNone(row["focus_hold_since"])
        self.assertTrue(any("pane focused" in event for event in events))

    def test_after_the_window_an_empty_composer_lets_the_push_through(self):
        """After the window an empty composer delivers and clears the hold."""
        radio.composer_has_draft = lambda provider, pane_id: False
        radio.relay_tick(self.conn)
        self.backdate_hold()
        radio.relay_tick(self.conn)
        self.assertEqual(self.pushes, ["w1:p1"])
        self.assertEqual(self.delivery()["status"], "delivered")
        self.assertIsNone(self.delivery()["focus_hold_since"])

    def test_after_the_window_a_draft_keeps_the_hold(self):
        """A detected draft keeps the delivery pending and reports user is typing."""
        radio.composer_has_draft = lambda provider, pane_id: True
        radio.relay_tick(self.conn)
        self.backdate_hold()
        events = radio.relay_tick(self.conn)
        self.assertEqual(self.pushes, [])
        self.assertEqual(self.delivery()["status"], "pending")
        self.assertTrue(any("user is typing" in event for event in events))


class DeliveryWaitReasonTest(RadioTestCase):
    """delivery_wait_reason: the blocked/working/booting defers plus the focus hold."""

    def handle_row(self, name):
        """Fetch the handles row for a name."""
        return self.conn.execute(
            "SELECT * FROM handles WHERE name = ?", (name,)
        ).fetchone()

    def test_reason_matrix(self):
        """Blocked and working defer; a booting agent defers within the grace window; others
        push."""
        self.add_handle("shell", ref="herdr:w1:p1", agent=None)
        self.add_handle("boot", ref="herdr:w1:p2", agent="claude", last_seen=radio.now())

        shell = self.handle_row("shell")
        boot = self.handle_row("boot")

        self.assertEqual(radio.delivery_wait_reason({"agent_status": "blocked"}, shell),
                         "agent blocked")
        self.assertEqual(radio.delivery_wait_reason({"agent_status": "working"}, shell),
                         "agent working")
        self.assertIsNone(radio.delivery_wait_reason({"agent": "claude"}, shell))
        self.assertIsNone(radio.delivery_wait_reason({}, shell))
        self.assertEqual(radio.delivery_wait_reason({}, boot), "agent still booting")

    def test_focus_holds_then_the_composer_decides(self):
        """Focused panes hold; after the window the composer verdict decides, and unfocused
        panes push."""
        saved_draft = radio.composer_has_draft
        self.addCleanup(setattr, radio, "composer_has_draft", saved_draft)
        self.add_handle("agent", ref="herdr:w1:p9", agent="codex")
        row = self.handle_row("agent")
        focused = {"agent_status": "idle", "agent": "codex", "focused": True}
        self.assertEqual(
            radio.delivery_wait_reason(focused, row, "w1:p9"),
            "pane focused — user is there",
        )
        self.assertEqual(
            radio.delivery_wait_reason(focused, row, "w1:p9", radio.now()),
            "pane focused — user is there",
        )
        old = (
            datetime.now(timezone.utc) - timedelta(seconds=radio.FOCUS_HOLD_S + 1)
        ).isoformat(timespec="milliseconds")
        radio.composer_has_draft = lambda provider, pane_id: False
        self.assertIsNone(radio.delivery_wait_reason(focused, row, "w1:p9", old))
        radio.composer_has_draft = lambda provider, pane_id: True
        self.assertEqual(
            radio.delivery_wait_reason(focused, row, "w1:p9", old),
            "pane focused — user is typing",
        )
        # Unfocused panes behave exactly as before.
        self.assertIsNone(
            radio.delivery_wait_reason({"agent_status": "idle", "agent": "codex"}, row, "w1:p9")
        )


class RelayTickTest(RadioTestCase):
    """relay_tick outcomes: delivered, no-pane pull, retry backoff and agent-blocked defers."""

    def setUp(self):
        """Seed bob on a pane with one pending PM."""
        super().setUp()
        self.add_handle("bob", ref="herdr:w1:p1")
        self.mid = self.pm("alice", "bob", "hi")
        self._fetch = radio.fetch_pane
        self._push = radio.push_to_pane
        self.addCleanup(self._restore)

    def _restore(self):
        """Put the stubbed pane functions back."""
        radio.fetch_pane = self._fetch
        radio.push_to_pane = self._push

    def tick(self, pane, push_result):
        """Run one relay tick with the given pane and push result."""
        radio.fetch_pane = lambda pane_id: pane
        radio.push_to_pane = lambda pane_id, text: push_result
        return radio.relay_tick(self.conn)

    def test_push_ok_marks_delivered(self):
        """A successful push marks the delivery delivered and reports it."""
        events = self.tick({"label": "bob"}, (True, None))
        row = self.delivery_row(self.mid)
        self.assertEqual(row["status"], "delivered")
        self.assertIsNotNone(row["delivered_at"])
        self.assertTrue(any("delivered" in e for e in events))

    def test_no_pane_falls_back_to_pull(self):
        """A vanished pane leaves the mail for pull with 'no live pane'."""
        self.tick(None, (True, None))
        row = self.delivery_row(self.mid)
        self.assertEqual(row["status"], "pull")
        self.assertEqual(row["last_error"], "no live pane")

    def test_generic_failure_counts_attempts_then_fails(self):
        """Failures count up to MAX_DELIVERY_ATTEMPTS and then go terminal."""
        pane = {"label": "bob"}
        self.tick(pane, (False, "agent_not_accepting"))
        row = self.delivery_row(self.mid)
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["status"], "pending")

        self.conn.execute("UPDATE deliveries SET attempts=? WHERE message_id=?",
                          (radio.MAX_DELIVERY_ATTEMPTS - 1, self.mid))
        self.conn.commit()
        # The first attempt just stamped last_attempt_at; the backoff window
        # would skip the retry, so back-date it to make the delivery due.
        self.backdate_attempt(self.mid, radio.RETRY_AFTER_S + 10)
        self.tick(pane, (False, "agent_not_accepting"))
        row = self.delivery_row(self.mid)
        self.assertEqual(row["attempts"], radio.MAX_DELIVERY_ATTEMPTS)
        self.assertEqual(row["status"], "failed")

    def test_agent_blocked_defers_without_attempt(self):
        """A blocked agent defers without consuming an attempt."""
        events = self.tick({"label": "bob"}, (False, "agent_blocked"))
        row = self.delivery_row(self.mid)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)
        self.assertTrue(any("agent blocked" in e for e in events))

    def test_booting_agent_defers_then_falls_through_to_typing(self):
        """A pane that has not reported its agent is booting, not reused: the
        delivery holds through the grace window and then types into the shell
        the pane actually is."""
        self.conn.execute("UPDATE handles SET agent='codex' WHERE name='bob'")
        self.conn.commit()
        events = self.tick({"label": "bob"}, (True, None))
        row = self.delivery_row(self.mid)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)
        self.assertTrue(any("agent still booting" in e for e in events))
        old = (
            datetime.now(timezone.utc) - timedelta(seconds=radio.AGENT_BOOT_GRACE_S + 5)
        ).isoformat()
        self.conn.execute("UPDATE handles SET last_seen=? WHERE name='bob'", (old,))
        self.conn.commit()
        self.tick({"label": "bob"}, (True, None))
        self.assertEqual(self.delivery_row(self.mid)["status"], "delivered")


class RelayIdentityGuardTest(RadioTestCase):
    """The relay must never push one handle's mail into a pane that no longer
    belongs to it — a live pane id proves nothing (panes get reused)."""

    def setUp(self):
        """Capture push calls and stub the pane functions."""
        super().setUp()
        self._fetch = radio.fetch_pane
        self._push = radio.push_to_pane
        self.addCleanup(self._restore)
        self.push_calls = []

    def _restore(self):
        """Put the stubbed pane functions back."""
        radio.fetch_pane = self._fetch
        radio.push_to_pane = self._push

    def tick(self, pane):
        """Run one relay tick against the given pane."""
        radio.fetch_pane = lambda pane_id: pane

        def push(pane_id, text):
            """Push stub that records the pane id and reports success."""
            self.push_calls.append(pane_id)
            return True, None

        radio.push_to_pane = push
        return radio.relay_tick(self.conn)

    def test_label_mismatch_leaves_for_pull_without_push(self):
        """A reused pane whose label changed is left for pull, never pushed."""
        self.add_handle("bob", ref="herdr:w1:p1")
        mid = self.pm("alice", "bob", "hi")
        self.tick({"label": "mallory"})
        row = self.delivery_row(mid)
        self.assertEqual(row["status"], "pull")
        self.assertEqual(row["last_error"], "pane reused")
        self.assertEqual(self.push_calls, [])

    def test_agent_mismatch_leaves_for_pull_without_push(self):
        """A pane now running a different agent is left for pull."""
        self.add_handle("bob", ref="herdr:w1:p1", agent="claude", agent_session="sess1")
        mid = self.pm("alice", "bob", "hi")
        self.tick({"label": "bob", "agent": "codex"})
        row = self.delivery_row(mid)
        self.assertEqual(row["status"], "pull")
        self.assertEqual(row["last_error"], "agent changed")
        self.assertEqual(self.push_calls, [])

    def test_matching_pane_gets_the_push(self):
        """A pane whose label and agent match still receives the push."""
        self.add_handle("bob", ref="herdr:w1:p1", agent="claude", agent_session="sess1")
        mid = self.pm("alice", "bob", "hi")
        self.tick({"label": "bob", "agent": "claude"})
        self.assertEqual(self.push_calls, ["w1:p1"])
        self.assertEqual(self.delivery_row(mid)["status"], "delivered")


class DeliveryUnconfirmedTest(RadioTestCase):
    """delivery_unconfirmed: the pane took the text but did not acknowledge, so it is left
    for pull and never retried."""

    def setUp(self):
        """Seed bob on a pane with one pending PM and capture pushes."""
        super().setUp()
        self.add_handle("bob", ref="herdr:w1:p1")
        self.mid = self.pm("alice", "bob", "hi")
        self._fetch = radio.fetch_pane
        self._push = radio.push_to_pane
        self.addCleanup(self._restore)
        self.push_calls = []

    def _restore(self):
        """Put the stubbed pane functions back."""
        radio.fetch_pane = self._fetch
        radio.push_to_pane = self._push

    def test_unconfirmed_goes_to_pull_and_is_never_retried(self):
        """An unconfirmed push becomes pull with no attempts and is not retried on a later tick."""
        radio.fetch_pane = lambda pane_id: {"label": "bob"}

        def push(pane_id, text):
            """Push stub that records the call and reports an unconfirmed delivery."""
            self.push_calls.append(pane_id)
            return False, "delivery_unconfirmed"

        radio.push_to_pane = push
        radio.relay_tick(self.conn)
        row = self.delivery_row(self.mid)
        self.assertEqual(row["status"], "pull")
        self.assertEqual(row["attempts"], 0)
        self.assertIn("unconfirmed", row["last_error"])
        self.assertEqual(len(self.push_calls), 1)
        # 'pull' is not pending: a later tick must not push this delivery again.
        radio.relay_tick(self.conn)
        self.assertEqual(len(self.push_calls), 1)


class PushToPaneTest(RadioTestCase):
    """push_to_pane: the error contract the relay switches on, and the shell
    escaping that keeps a typed message inert."""

    def setUp(self):
        """Stub herdr and the paste delay so each branch runs without a pane."""
        super().setUp()
        saved_herdr = radio.herdr
        self.addCleanup(setattr, radio, "herdr", saved_herdr)
        saved_sleep = radio.time.sleep
        self.addCleanup(setattr, radio.time, "sleep", saved_sleep)
        radio.time.sleep = lambda seconds: None
        self.calls = []

    def install(self, *, returncode=0, stderr="", raises=None):
        """Answer every herdr call with one configured result or exception."""
        def fake(*args, **kwargs):
            """Record the call and answer with the configured result."""
            self.calls.append(args)
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(list(args), returncode, stdout="", stderr=stderr)
        radio.herdr = fake

    def shell_calls(self):
        """The send-text calls the shell branch made."""
        return [call for call in self.calls if call[:2] == ("pane", "send-text")]

    def test_prompt_timeout_is_unconfirmed(self):
        """A wedged herdr must never turn into a resubmission."""
        self.install(raises=subprocess.TimeoutExpired("herdr", 7))
        self.assertEqual(radio.push_to_pane("w1:p1", "hi"), (False, "delivery_unconfirmed"))

    def test_blocked_and_stalled_map_to_their_sentinels(self):
        """herdr's stderr wording maps to the contract the relay switches on."""
        self.install(returncode=1, stderr="agent is blocked by a dialog")
        self.assertEqual(radio.push_to_pane("w1:p1", "hi"), (False, "agent_blocked"))
        self.install(returncode=1, stderr="prompt stalled after 6s")
        self.assertEqual(radio.push_to_pane("w1:p1", "hi"), (False, "delivery_unconfirmed"))

    def test_agent_pane_gets_the_raw_text(self):
        """A detected agent receives the message untouched."""
        self.install()
        radio.push_to_pane("w1:p1", "rm -rf / ; echo hi")
        self.assertEqual(self.calls[0][3], "rm -rf / ; echo hi")
        self.assertEqual(self.shell_calls(), [])

    def test_shell_pane_gets_escaped_flattened_text(self):
        """A plain shell receives one inert line: no execution, no redirection."""
        self.install(returncode=1, stderr="not an agent pane")
        radio.push_to_pane("w1:p1", "a; b $(x)\nnext > ~/out")
        text = self.shell_calls()[0][3]
        self.assertNotIn("\n", text)
        self.assertIn("a\\;", text)
        self.assertIn("\\$", text)
        self.assertIn("\\>", text)

    def test_shell_safe_text_uses_the_platform_escape(self):
        """POSIX escapes with a backslash; Windows uses PowerShell's backtick."""
        self.assertEqual(radio.shell_safe_text("a;b `c`"), "a\\;b \\`c\\`")
        saved = radio.os.name
        self.addCleanup(setattr, radio.os, "name", saved)
        radio.os.name = "nt"
        self.assertEqual(radio.shell_safe_text("a;b"), "a`;b")


class BacktickNormalizationTest(RadioTestCase):
    """Typed text is normalized before it is stored."""

    def test_backticks_become_quotes(self):
        # A backtick typed into a shell pane is command substitution.
        """Backticks become single quotes so a shell pane never runs command substitution."""
        mid = self.pm("alice", "bob", "run `ls -la` now")
        self.assertEqual(self.message_row(mid)["text"], "run 'ls -la' now")


class NormalizeHandleTest(RadioTestCase):
    """normalize_handle: the @ prefix is accepted, invalid names exit."""

    def test_strip_at_and_validate(self):
        """@foo and foo normalize; spaces and brackets are rejected."""
        self.assertEqual(radio.normalize_handle("@foo"), "foo")
        self.assertEqual(radio.normalize_handle("foo"), "foo")
        with self.assertRaises(SystemExit):
            radio.normalize_handle("bad handle")
        with self.assertRaises(SystemExit):
            radio.normalize_handle("we]rd")

    def test_pm_to_at_handle_resolves(self):
        """pm to @bob resolves to the registered bob."""
        self.add_handle("bob")
        args = argparse.Namespace(sender="alice", to="@bob", text=["hi"],
                                  ref=None, reply_required=False)
        with contextlib.redirect_stdout(io.StringIO()):
            radio.cmd_pm(self.conn, args)
        row = self.conn.execute("SELECT * FROM messages").fetchone()
        self.assertEqual(row["to_handle"], "bob")


def join_args(handle, pane=None, role=None, account=None):
    """Build the argparse namespace cmd_join expects, with no_launch on."""
    return argparse.Namespace(handle=handle, pane=pane, provider=None,
                              new=False, resume=False, model=None, role=role,
                              account=account, no_launch=True)


class ResolveHandleTest(RadioTestCase):
    """resolve_handle: case-insensitive lookup that preserves the registered spelling."""

    def test_resolver_semantics(self):
        """Exact and case-insensitive hits resolve; a miss returns None."""
        self.add_handle("bob")
        self.assertEqual(radio.resolve_handle(self.conn, "bob")["name"], "bob")
        self.assertEqual(radio.resolve_handle(self.conn, "BOB")["name"], "bob")
        self.assertIsNone(radio.resolve_handle(self.conn, "ghost"))

    def test_ambiguous_case_variants_exit(self):
        """Two case variants make an ambiguous lookup exit."""
        self.add_handle("Foo")
        self.add_handle("foo")
        with self.assertRaises(SystemExit):
            radio.resolve_handle(self.conn, "FOO")

    def test_pm_case_insensitive_uses_registered_spelling(self):
        """pm resolves BOB to the stored 'bob'."""
        self.add_handle("bob")
        args = argparse.Namespace(sender="alice", to="BOB", text=["hi"],
                                  ref=None, reply_required=False)
        with contextlib.redirect_stdout(io.StringIO()):
            radio.cmd_pm(self.conn, args)
        row = self.conn.execute("SELECT * FROM messages").fetchone()
        self.assertEqual(row["to_handle"], "bob")

    def test_part_wrong_case_removes_the_right_row(self):
        """part BOB removes the bob row."""
        self.add_handle("bob")
        with contextlib.redirect_stdout(io.StringIO()):
            radio.cmd_part(self.conn, argparse.Namespace(handle="BOB"))
        row = self.conn.execute("SELECT name FROM handles").fetchone()
        self.assertIsNone(row)

    def test_join_case_variant_upserts_existing(self):
        """Joining BOB does not fork a second row for bob."""
        self.add_handle("bob", ref="manual")
        with contextlib.redirect_stdout(io.StringIO()):
            radio.cmd_join(self.conn, join_args("BOB"))
        rows = self.conn.execute(
            "SELECT name FROM handles WHERE LOWER(name) = 'bob'"
        ).fetchall()
        self.assertEqual([r["name"] for r in rows], ["bob"])


class RepairTest(RadioTestCase):
    """radio repair: a health report and --reset with a backup — the supported
    way out of a corrupt ledger or one written by a newer radio."""

    def run_repair(self, reset=False, yes=False):
        """Run cmd_repair with stdout captured."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = radio.cmd_repair(argparse.Namespace(reset=reset, yes=yes))
        return rc, buf.getvalue()

    def test_health_report_on_a_fresh_ledger(self):
        """A fresh ledger reports healthy and the current schema."""
        rc, out = self.run_repair()
        self.assertEqual(rc, 0)
        self.assertIn("verdict:   healthy", out)
        self.assertIn(f"schema:    {radio.SCHEMA_VERSION}", out)

    def test_newer_schema_is_reported_and_connect_refuses(self):
        """A newer schema is reported with the reset hint and connect refuses."""
        self.conn.execute("PRAGMA user_version = 99")
        self.conn.commit()
        rc, out = self.run_repair()
        self.assertEqual(rc, 1)
        self.assertIn("newer", out)
        self.assertIn("radio repair --reset", out)
        with self.assertRaises(SystemExit) as ctx:
            radio.connect()
        self.assertIn("newer radio", str(ctx.exception))

    def test_reset_requires_yes_when_not_interactive(self):
        """A non-interactive reset without --yes exits."""
        saved = sys.stdin
        sys.stdin = io.StringIO("")
        self.addCleanup(setattr, sys, "stdin", saved)
        with self.assertRaises(SystemExit):
            self.run_repair(reset=True)

    def test_reset_recovers_from_a_newer_schema(self):
        """reset --yes recreates the ledger at the current schema."""
        self.conn.execute("PRAGMA user_version = 99")
        self.conn.commit()
        rc, out = self.run_repair(reset=True, yes=True)
        self.assertEqual(rc, 0)
        fresh = radio.connect()
        self.addCleanup(fresh.close)
        self.assertEqual(
            fresh.execute("PRAGMA user_version").fetchone()[0], radio.SCHEMA_VERSION
        )

    def test_reset_keeps_a_backup_and_creates_an_empty_ledger(self):
        """reset backs the old ledger up and leaves an empty one."""
        self.add_handle("bob")
        self.pm("alice", "bob", "hi")
        rc, out = self.run_repair(reset=True, yes=True)
        self.assertEqual(rc, 0)
        self.assertIn("backup:", out)
        self.assertIn("fresh:", out)
        backups = list(radio.STATE_DIR.glob("radio.db.bak-*"))
        self.assertEqual(len(backups), 1)
        backup = sqlite3.connect(backups[0])
        self.addCleanup(backup.close)
        names = [r[0] for r in backup.execute("SELECT name FROM handles")]
        self.assertEqual(names, ["bob"])
        fresh = radio.connect()
        self.addCleanup(fresh.close)
        self.assertEqual(fresh.execute("SELECT COUNT(*) AS c FROM handles").fetchone()["c"], 0)
        self.assertEqual(fresh.execute("PRAGMA user_version").fetchone()[0], radio.SCHEMA_VERSION)


class WorkspaceScopeTest(RadioTestCase):
    """Scoped identity: one name per workspace, resolution inside a workspace,
    the unscoped fallback, and the internal qualified form scripts use."""

    WS_LABELS = {
        "result": {
            "workspaces": [
                {"workspace_id": "w1", "label": "BoilerRoom"},
                {"workspace_id": "w2", "label": "Servers"},
            ]
        }
    }

    def stub_workspaces(self, data=None):
        """Stub herdr_json with the workspace label fixture."""
        saved = radio.herdr_json
        self.addCleanup(setattr, radio, "herdr_json", saved)
        radio.herdr_json = lambda *args: (data if data is not None else self.WS_LABELS)

    def test_same_name_in_two_workspaces(self):
        """The same name resolves to its own workspace's row."""
        self.add_handle("reviewer", workspace="w1")
        self.add_handle("reviewer", workspace="w2")
        row = radio.resolve_handle(self.conn, "reviewer", "w2")
        self.assertEqual((row["workspace"], row["name"]), ("w2", "reviewer"))

    def test_other_workspace_handle_is_unreachable(self):
        """A handle in another workspace does not resolve."""
        self.add_handle("hede", workspace="w1")
        self.assertIsNone(radio.resolve_handle(self.conn, "hede", "w2"))

    def test_unscoped_handles_stay_reachable(self):
        """Unscoped legacy rows resolve from any workspace."""
        self.add_handle("bot")
        self.add_handle("reviewer", workspace="w2")
        self.assertEqual(radio.resolve_handle(self.conn, "bot", "w2")["workspace"], "")

    def test_internal_qualified_form(self):
        """The w1:name form resolves across scopes for scripts."""
        self.add_handle("reviewer", workspace="w1")
        row = radio.resolve_handle(self.conn, "w1:reviewer", "")
        self.assertEqual(row["workspace"], "w1")

    def test_outside_scope_ambiguity_names_candidates(self):
        """Outside a workspace an ambiguous name lists both qualified candidates."""
        self.add_handle("reviewer", workspace="w1")
        self.add_handle("reviewer", workspace="w2")
        with self.assertRaises(SystemExit) as ctx:
            radio.resolve_handle(self.conn, "reviewer", "")
        self.assertIn("w1:reviewer", str(ctx.exception))
        self.assertIn("w2:reviewer", str(ctx.exception))

    def test_miss_message_names_the_workspace(self):
        """A scoped miss names the workspace and points at the other one."""
        self.stub_workspaces()
        self.add_handle("hede", workspace="w1")
        message = radio.no_handle_message(self.conn, "hede", "w2")
        self.assertIn('no handle "hede"', message)
        self.assertIn("Servers", message)
        self.assertIn("BoilerRoom", message)

    def test_workspace_spec_accepts_id_and_label(self):
        """An explicit workspace accepts either the id or the label."""
        self.stub_workspaces()
        self.assertEqual(radio.resolve_workspace_spec("Servers", self.conn), "w2")
        self.assertEqual(radio.resolve_workspace_spec("w1", self.conn), "w1")
        with self.assertRaises(SystemExit):
            radio.resolve_workspace_spec("Nope", self.conn)


class ScopedPmTest(RadioTestCase):
    """cmd_pm routes inside the sender's workspace and refuses to cross."""

    def setUp(self):
        """Pretend the command runs in w2 with RADIO_HANDLE=alice."""
        super().setUp()
        saved = radio.current_workspace
        self.addCleanup(setattr, radio, "current_workspace", saved)
        radio.current_workspace = lambda: "w2"
        os.environ["RADIO_HANDLE"] = "alice"
        self.addCleanup(os.environ.pop, "RADIO_HANDLE", None)
        saved_json = radio.herdr_json
        self.addCleanup(setattr, radio, "herdr_json", saved_json)
        radio.herdr_json = lambda *args: {}

    @staticmethod
    def pm_args(to):
        """Build a cmd_pm namespace with no explicit sender (it resolves from the environment)."""
        return argparse.Namespace(sender=None, to=to, text=["hi"], ref=None,
                                  reply_required=False)

    def test_pm_resolves_in_own_workspace(self):
        """Both sides resolve inside the sender's workspace."""
        self.add_handle("alice", workspace="w2")
        self.add_handle("reviewer", workspace="w1")
        self.add_handle("reviewer", workspace="w2")
        with contextlib.redirect_stdout(io.StringIO()):
            radio.cmd_pm(self.conn, self.pm_args("reviewer"))
        row = self.conn.execute("SELECT * FROM messages").fetchone()
        self.assertEqual((row["from_ws"], row["from_handle"]), ("w2", "alice"))
        self.assertEqual((row["to_ws"], row["to_handle"]), ("w2", "reviewer"))

    def test_pm_refuses_other_workspace(self):
        """A handle that exists only elsewhere is reported as missing, naming this workspace."""
        self.add_handle("alice", workspace="w2")
        self.add_handle("hede", workspace="w1")
        with self.assertRaises(SystemExit) as ctx:
            radio.cmd_pm(self.conn, self.pm_args("hede"))
        message = str(ctx.exception)
        self.assertIn('no handle "hede"', message)
        self.assertIn("w2", message)

    def test_pm_warns_when_the_sender_is_not_joined(self):
        """An unjoined sender still sends, with a warning."""
        self.add_handle("bob", workspace="w2")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            radio.cmd_pm(
                self.conn,
                argparse.Namespace(sender="host", to="bob", text=["hi"],
                                   ref=None, reply_required=False),
            )
        self.assertIn("not a joined handle", buf.getvalue())
        self.assertEqual(
            self.conn.execute("SELECT from_handle FROM messages").fetchone()["from_handle"],
            "host",
        )

    def test_pm_accepts_internal_qualified_form(self):
        # Scripts outside herdr address a scoped handle as w1:name.
        """A script can address w1:bob and the delivery records w1."""
        self.add_handle("bob", workspace="w1")
        os.environ.pop("RADIO_HANDLE")
        with contextlib.redirect_stdout(io.StringIO()):
            radio.cmd_pm(
                self.conn,
                argparse.Namespace(sender="host", to="w1:bob", text=["hi"],
                                   ref=None, reply_required=False),
            )
        row = self.conn.execute("SELECT * FROM messages").fetchone()
        self.assertEqual((row["to_ws"], row["to_handle"]), ("w1", "bob"))


class ScopedDeliveryTest(RadioTestCase):
    """Deliveries carry the target workspace, and the relay refuses a pane
    that lives in a different workspace."""

    def _stub_panes(self, pane):
        """Stub the pane fetch/push calls and capture pushes."""
        for name in ("fetch_pane", "push_to_pane"):
            saved = getattr(radio, name)
            self.addCleanup(setattr, radio, name, saved)
        radio.fetch_pane = lambda pane_id: pane
        self.pushed = []
        radio.push_to_pane = lambda pane_id, text: (self.pushed.append(pane_id), (True, None))[1]

    def test_delivery_records_target_workspace(self):
        """The delivery carries the target workspace."""
        self.add_handle("bob", ref="herdr:w2:p1", workspace="w2")
        mid = self.pm("alice", "bob", "hi", to_ws="w2")
        self.assertEqual(self.delivery_row(mid)["target_ws"], "w2")
        self.assertEqual(self.delivery_row(mid)["status"], "pending")

    def test_relay_pushes_inside_the_workspace(self):
        """A pane in the right workspace receives the push."""
        self.add_handle("bob", ref="herdr:w2:p1", workspace="w2")
        mid = self.pm("alice", "bob", "hi", to_ws="w2")
        self._stub_panes({"label": "bob", "workspace_id": "w2"})
        radio.relay_tick(self.conn)
        self.assertEqual(self.pushed, ["w2:p1"])
        self.assertEqual(self.delivery_row(mid)["status"], "delivered")

    def test_relay_refuses_foreign_workspace(self):
        """A pane that moved to another workspace is left for pull."""
        self.add_handle("bob", ref="herdr:w9:p1", workspace="w2")
        mid = self.pm("alice", "bob", "hi", to_ws="w2")
        self._stub_panes({"label": "bob", "workspace_id": "w9"})
        radio.relay_tick(self.conn)
        row = self.delivery_row(mid)
        self.assertEqual(row["status"], "pull")
        self.assertEqual(row["last_error"], "workspace changed")
        self.assertEqual(self.pushed, [])


class RoleTest(RadioTestCase):
    """Roles are per-workspace ledger state: set/show/clear, scoped like every
    other handle operation, and carried by the briefing."""

    def setUp(self):
        """Pretend the command runs in w2."""
        super().setUp()
        saved = radio.current_workspace
        self.addCleanup(setattr, radio, "current_workspace", saved)
        radio.current_workspace = lambda: "w2"
        saved_json = radio.herdr_json
        self.addCleanup(setattr, radio, "herdr_json", saved_json)
        radio.herdr_json = lambda *args: {}

    def run_role(self, handle, text=(), clear=False):
        """Run cmd_role with stdout captured."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            radio.cmd_role(
                self.conn,
                argparse.Namespace(handle=handle, text=list(text), clear=clear),
            )
        return buf.getvalue()

    def test_set_show_clear(self):
        """Setting, showing and clearing a role round-trips through the ledger."""
        self.add_handle("reviewer", workspace="w2")
        self.run_role("reviewer", ["test", "evidence", "gate"])
        row = self.conn.execute(
            "SELECT role FROM handles WHERE workspace='w2' AND name='reviewer'"
        ).fetchone()
        self.assertEqual(row["role"], "test evidence gate")
        self.assertIn("test evidence gate", self.run_role("reviewer"))
        self.assertIn("cleared", self.run_role("reviewer", clear=True))
        row = self.conn.execute(
            "SELECT role FROM handles WHERE workspace='w2' AND name='reviewer'"
        ).fetchone()
        self.assertIsNone(row["role"])

    def test_role_is_scoped(self):
        """A handle in another workspace cannot be given a role."""
        self.add_handle("reviewer", workspace="w1")
        with self.assertRaises(SystemExit):
            self.run_role("reviewer", ["nope"])

    def test_briefing_carries_workspace_and_role(self):
        """The briefing names the workspace label and the role."""
        radio.herdr_json = lambda *args: {
            "result": {"workspaces": [{"workspace_id": "w2", "label": "Servers"}]}
        }
        text = radio.briefing_text("reviewer", "w2", "Test evidence gate")
        self.assertIn('You are on Radio as "reviewer" in workspace "Servers"', text)
        self.assertIn("Your role: Test evidence gate", text)
        self.assertIn("scoped to this workspace", text)


class ScopedJoinTest(RadioTestCase):
    """join records the pane's workspace, keeps --role, and adopts pre-scope
    rows instead of forking a duplicate identity."""

    def setUp(self):
        """Stub herdr and the pane lookup with two same-name panes in different workspaces."""
        super().setUp()
        saved_herdr = radio.herdr
        self.addCleanup(setattr, radio, "herdr", saved_herdr)
        radio.herdr = lambda *args, **kwargs: subprocess.CompletedProcess(
            list(args), 0, stdout="", stderr=""
        )
        saved_fetch = radio.fetch_pane
        self.addCleanup(setattr, radio, "fetch_pane", saved_fetch)
        self.panes = {
            "w2:p1": {"label": "reviewer", "workspace_id": "w2"},
            "w1:p1": {"label": "reviewer", "workspace_id": "w1"},
        }
        radio.fetch_pane = lambda pane_id: self.panes.get(pane_id)

    def test_join_records_workspace_and_role(self):
        """join records the pane's workspace and keeps --role."""
        with contextlib.redirect_stdout(io.StringIO()):
            radio.cmd_join(self.conn, join_args("reviewer", pane="w2:p1",
                                                role="test evidence gate"))
        row = self.conn.execute("SELECT * FROM handles").fetchone()
        self.assertEqual((row["workspace"], row["name"]), ("w2", "reviewer"))
        self.assertEqual(row["role"], "test evidence gate")

    def test_same_name_joins_in_two_workspaces(self):
        """The same name can join once per workspace."""
        for pane_id in ("w2:p1", "w1:p1"):
            with contextlib.redirect_stdout(io.StringIO()):
                radio.cmd_join(self.conn, join_args("reviewer", pane=pane_id))
        rows = self.conn.execute("SELECT workspace FROM handles ORDER BY workspace").fetchall()
        self.assertEqual([r["workspace"] for r in rows], ["w1", "w2"])

    def test_pre_scope_row_is_adopted(self):
        """A pre-scope row is adopted into the workspace instead of duplicated."""
        self.add_handle("reviewer", ref="herdr:w2:p1")  # pre-scope: workspace ''
        with contextlib.redirect_stdout(io.StringIO()):
            radio.cmd_join(self.conn, join_args("reviewer", pane="w2:p1"))
        rows = self.conn.execute("SELECT workspace, name FROM handles").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["workspace"], "w2")

    def test_flagless_rejoin_keeps_the_recorded_provider(self):
        """A plain rejoin (or a bot's --no-launch) must not erase the provider."""
        self.add_handle("reviewer", ref="herdr:w2:p1", agent="claude",
                        agent_session="sess1", workspace="w2")
        with contextlib.redirect_stdout(io.StringIO()):
            radio.cmd_join(self.conn, join_args("reviewer", pane="w2:p1"))
        row = self.conn.execute(
            "SELECT * FROM handles WHERE workspace='w2' AND name='reviewer'"
        ).fetchone()
        self.assertEqual(row["agent"], "claude")
        self.assertEqual(row["agent_session"], "sess1")


class AccountTest(RadioTestCase):
    """Named provider accounts: several logins of one provider, recorded on the
    handle so restore brings the same login back."""

    def setUp(self):
        """Stub herdr and the pane lookup for a w2 coder pane."""
        super().setUp()
        saved_herdr = radio.herdr
        self.addCleanup(setattr, radio, "herdr", saved_herdr)
        radio.herdr = lambda *args, **kwargs: subprocess.CompletedProcess(
            list(args), 0, stdout="", stderr=""
        )
        saved_fetch = radio.fetch_pane
        self.addCleanup(setattr, radio, "fetch_pane", saved_fetch)
        self.panes = {"w2:p1": {"label": "coder", "workspace_id": "w2"}}
        radio.fetch_pane = lambda pane_id: self.panes.get(pane_id)

    def run_account(self, *argv):
        """Parse and run an account subcommand with stdout captured."""
        buf = io.StringIO()
        args = radio.build_parser().parse_args(["account", *argv])
        with contextlib.redirect_stdout(buf):
            rc = radio.cmd_account(self.conn, args)
        return rc, buf.getvalue()

    def test_default_homes_follow_the_provider_convention(self):
        """Default homes follow the provider convention, numbered for extra accounts."""
        self.assertEqual(radio.default_account_home("codex", "codex"), Path.home() / ".codex")
        self.assertEqual(
            radio.default_account_home("codex", "codex2"), Path.home() / ".codex-account-2"
        )
        self.assertEqual(
            radio.default_account_home("claude", "claude3"), Path.home() / ".claude-account-3"
        )

    def test_account_env_combines_home_and_overrides(self):
        """The provider's home variable plus overrides, with overrides winning."""
        self.assertEqual(
            radio.account_env({"provider": "codex", "home": r"C:\x", "env": "{}"}),
            {"CODEX_HOME": r"C:\x"},
        )
        self.assertEqual(
            radio.account_env({"provider": "claude", "home": "/x", "env": "{}"}),
            {"CLAUDE_CONFIG_DIR": "/x"},
        )
        self.assertEqual(
            radio.account_env({"provider": "kimi", "home": "/x", "env": "{}"}),
            {"KIMI_CODE_HOME": "/x", "KIMI_HOME": "/x"},
        )
        # Overrides can add any variable, and they win over the home mapping.
        self.assertEqual(
            radio.account_env(
                {"provider": "codex", "home": "/x", "env": '{"HTTPS_PROXY": "http://p"}'}
            ),
            {"CODEX_HOME": "/x", "HTTPS_PROXY": "http://p"},
        )
        self.assertEqual(
            radio.account_env({"provider": "codex", "home": "/x", "env": '{"CODEX_HOME": "/y"}'}),
            {"CODEX_HOME": "/y"},
        )
        # No home: only the overrides apply (the provider default is used).
        self.assertEqual(
            radio.account_env({"provider": "codex", "home": None, "env": '{"A": "1"}'}),
            {"A": "1"},
        )

    def test_add_list_remove(self):
        """add records and creates the home, list shows it, remove deletes it."""
        tmp_home = str(radio.STATE_DIR / "codex2-home")
        rc, out = self.run_account("add", "codex2", "--provider", "codex", "--home", tmp_home)
        self.assertEqual(rc, 0)
        self.assertIn("codex2", out)
        row = self.conn.execute("SELECT * FROM accounts WHERE name='codex2'").fetchone()
        self.assertEqual(row["provider"], "codex")
        self.assertEqual(row["home"], tmp_home)
        self.assertTrue(Path(tmp_home).is_dir())  # the home is created for login
        rc, out = self.run_account("list")
        self.assertIn("codex2", out)
        self.assertIn(row["home"], out)
        rc, out = self.run_account("remove", "codex2")
        self.assertIn("removed", out)
        self.assertIsNone(self.conn.execute("SELECT * FROM accounts").fetchone())

    def test_env_overrides_are_recorded_and_listed(self):
        """K=V overrides are stored, listed, and malformed ones rejected."""
        tmp_home = str(radio.STATE_DIR / "proxy-home")
        rc, out = self.run_account(
            "add", "proxy", "--provider", "codex", "--home", tmp_home,
            "--env", "HTTPS_PROXY=http://p", "--env", "FOO=bar",
        )
        self.assertIn("HTTPS_PROXY=http://p", out)
        row = self.conn.execute("SELECT * FROM accounts WHERE name='proxy'").fetchone()
        self.assertEqual(json.loads(row["env"]), {"HTTPS_PROXY": "http://p", "FOO": "bar"})
        rc, out = self.run_account("list")
        # Names by default: an account's environment may carry a token.
        self.assertIn("HTTPS_PROXY", out)
        self.assertNotIn("http://p", out)
        rc, out = self.run_account("list", "--show-env")
        self.assertIn("HTTPS_PROXY=http://p", out)
        with self.assertRaises(SystemExit):
            self.run_account("add", "bad", "--provider", "codex", "--env", "NOVALUE")

    def test_duplicate_and_in_use_guards(self):
        """Duplicate names and removing an in-use account exit."""
        self.run_account(
            "add", "codex2", "--provider", "codex", "--home", str(radio.STATE_DIR / "codex2-home")
        )
        with self.assertRaises(SystemExit):
            self.run_account("add", "codex2", "--provider", "codex")
        self.add_handle("coder", ref="herdr:w2:p1", workspace="w2", agent="codex")
        self.conn.execute("UPDATE handles SET account='codex2' WHERE name='coder'")
        self.conn.commit()
        with self.assertRaises(SystemExit) as ctx:
            self.run_account("remove", "codex2")
        self.assertIn("in use by: coder", str(ctx.exception))

    def test_roster_shows_the_account_separately(self):
        """The roster shows agent and account separately, never fused."""
        self.add_handle("coder", ref="manual", workspace="w2", agent="codex", account="work")
        row = self.conn.execute("SELECT * FROM handles WHERE name='coder'").fetchone()
        line = radio.handle_line(row)
        self.assertIn("codex", line)
        self.assertIn("account: work", line)
        self.assertNotIn("codex/work", line)

    def test_move_copies_the_session_and_switches_the_account(self):
        """move copies the session file and index into the target home and switches the
        handle; the source stays."""
        source_home = radio.STATE_DIR / "codex-home"
        target_home = radio.STATE_DIR / "codex-work"
        session_file = source_home / "sessions" / "2026" / "09" / "rollout-x-sess-1.jsonl"
        session_file.parent.mkdir(parents=True)
        session_file.write_text('{"type": "session"}\n')
        (source_home / "session_index.jsonl").write_text('{"id": "sess-1", "thread_name": "coder"}\n')
        self.run_account("add", "personal", "--provider", "codex", "--home", str(source_home))
        self.run_account("add", "work", "--provider", "codex", "--home", str(target_home))
        self.add_handle("coder", ref="herdr:w2:p1", workspace="w2", agent="codex",
                        agent_session="sess-1", account="personal")
        args = radio.build_parser().parse_args(["account", "move", "coder", "--to", "work"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = radio.cmd_account(self.conn, args)
        self.assertEqual(rc, 0)
        self.assertIn("personal -> work", buf.getvalue())
        row = self.conn.execute("SELECT * FROM handles WHERE name='coder'").fetchone()
        self.assertEqual(row["account"], "work")
        copied = target_home / "sessions" / "2026" / "09" / "rollout-x-sess-1.jsonl"
        self.assertTrue(copied.exists())
        self.assertIn("sess-1", (target_home / "session_index.jsonl").read_text(encoding="utf-8"))
        # a copy, never a move: the old account keeps its data
        self.assertTrue(session_file.exists())

    def test_move_refuses_unknown_account_and_missing_sessions(self):
        """An unknown target exits; a session missing from the source home exits."""
        self.add_handle("coder", ref="herdr:w2:p1", workspace="w2", agent="codex",
                        agent_session="sess-1")
        args = radio.build_parser().parse_args(["account", "move", "coder", "--to", "ghost"])
        with self.assertRaises(SystemExit):
            radio.cmd_account(self.conn, args)
        self.run_account(
            "add", "work", "--provider", "codex", "--home", str(radio.STATE_DIR / "codex-work")
        )
        args = radio.build_parser().parse_args(["account", "move", "coder", "--to", "work"])
        with self.assertRaises(SystemExit) as ctx:
            radio.cmd_account(self.conn, args)
        self.assertIn("not found", str(ctx.exception))

    def test_move_without_a_session_just_switches(self):
        """A handle with no session just changes its account."""
        self.run_account(
            "add", "work", "--provider", "codex", "--home", str(radio.STATE_DIR / "codex-work")
        )
        self.add_handle("coder", ref="herdr:w2:p1", workspace="w2", agent="codex")
        args = radio.build_parser().parse_args(["account", "move", "coder", "--to", "work"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = radio.cmd_account(self.conn, args)
        self.assertEqual(rc, 0)
        self.assertIn("account changed only", buf.getvalue())

    def test_join_with_a_new_provider_drops_the_recorded_session(self):
        """Switching provider clears the recorded session and says it will not resume."""
        self.add_handle("coder", ref="herdr:w2:p1", workspace="w2", agent="claude",
                        agent_session="claude-1")
        args = join_args("coder", pane="w2:p1")
        args.provider = "codex"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            radio.cmd_join(self.conn, args)
        row = self.conn.execute("SELECT * FROM handles WHERE name='coder'").fetchone()
        self.assertEqual(row["agent"], "codex")
        self.assertIsNone(row["agent_session"])
        self.assertIn("not resumed under codex", buf.getvalue())

    def test_join_with_a_new_account_drops_the_recorded_session(self):
        """Switching account clears the session and points at account move."""
        self.run_account(
            "add", "work", "--provider", "codex", "--home", str(radio.STATE_DIR / "codex-work")
        )
        self.add_handle("coder", ref="herdr:w2:p1", workspace="w2", agent="codex",
                        agent_session="sess-1")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            radio.cmd_join(self.conn, join_args("coder", pane="w2:p1", account="work"))
        row = self.conn.execute("SELECT * FROM handles WHERE name='coder'").fetchone()
        self.assertEqual(row["account"], "work")
        self.assertIsNone(row["agent_session"])
        self.assertIn("radio account move coder --to work", buf.getvalue())

    def test_move_copies_claude_kimi_and_pi_layouts(self):
        """claude, kimi and pi session layouts are copied too."""
        cases = [
            ("claude", "projects/-x/abc.jsonl"),
            ("kimi", "sessions/wd_1/session_abc/state.json"),
            ("pi", "sessions/x/abc.jsonl"),
        ]
        for index, (provider, relative) in enumerate(cases):
            source = radio.STATE_DIR / f"src-{provider}"
            target = radio.STATE_DIR / f"dst-{provider}"
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"type": "session", "id": "abc"}\n')
            self.run_account("add", f"s{index}", "--provider", provider, "--home", str(source))
            self.run_account("add", f"t{index}", "--provider", provider, "--home", str(target))
            self.add_handle(f"h{index}", ref="manual", workspace="w2", agent=provider,
                            agent_session="abc", account=f"s{index}")
            args = radio.build_parser().parse_args(
                ["account", "move", f"h{index}", "--to", f"t{index}"]
            )
            with contextlib.redirect_stdout(io.StringIO()):
                rc = radio.cmd_account(self.conn, args)
            self.assertEqual(rc, 0, provider)
            self.assertTrue((target / relative).exists(), f"{provider}: {relative}")

    def test_kimi_probes_prefer_kimi_code_home(self):
        """auth_status for kimi checks KIMI_CODE_HOME before KIMI_HOME."""
        tmp = radio.STATE_DIR / "kimi-home"
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / "config.toml").write_text("")
        saved = {key: os.environ.get(key) for key in ("KIMI_CODE_HOME", "KIMI_HOME")}
        self.addCleanup(self._restore_env, saved)
        os.environ["KIMI_CODE_HOME"] = str(tmp)
        os.environ.pop("KIMI_HOME", None)
        self.assertEqual(radio.auth_status("kimi"), "configured")

    @staticmethod
    def _restore_env(saved):
        """Put the environment variables the test changed back."""
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_join_with_account_records_it(self):
        """join --account records provider and account on the handle."""
        self.run_account(
            "add", "codex2", "--provider", "codex", "--home", str(radio.STATE_DIR / "codex2-home")
        )
        with contextlib.redirect_stdout(io.StringIO()):
            radio.cmd_join(self.conn, join_args("coder", pane="w2:p1", account="codex2"))
        row = self.conn.execute("SELECT * FROM handles WHERE name='coder'").fetchone()
        self.assertEqual((row["agent"], row["account"]), ("codex", "codex2"))

    def test_join_rejects_unknown_account_and_provider_conflict(self):
        """An unknown account, or one whose provider conflicts, exits."""
        with self.assertRaises(SystemExit):
            radio.cmd_join(self.conn, join_args("coder", pane="w2:p1", account="ghost"))
        self.run_account(
            "add", "codex2", "--provider", "codex", "--home", str(radio.STATE_DIR / "codex2-home")
        )
        args = join_args("coder", pane="w2:p1", account="codex2")
        args.provider = "claude"
        with self.assertRaises(SystemExit):
            radio.cmd_join(self.conn, args)


class RestoreTest(RadioTestCase):
    """radio restore: verify the pane still belongs to the handle, then type
    the join/resume command into it; never creates layout."""

    def setUp(self):
        """Stub herdr and the pane lookup; seed coder with a recorded session."""
        super().setUp()
        self._herdr = radio.herdr
        self._fetch = radio.fetch_pane
        self.addCleanup(setattr, radio, "herdr", self._herdr)
        self.addCleanup(setattr, radio, "fetch_pane", self._fetch)
        self.calls = []

        def fake_herdr(*args, **kwargs):
            """herdr stub that records each argv and returns success."""
            self.calls.append(args)
            return subprocess.CompletedProcess(list(args), 0, stdout="", stderr="")

        radio.herdr = fake_herdr
        self.panes = {}
        radio.fetch_pane = lambda pane_id: self.panes.get(pane_id)
        self.add_handle("coder", ref="herdr:w2:p1", workspace="w2",
                        agent="codex", agent_session="sess-1")

    def restore(self, handle="coder"):
        """Run cmd_restore with stdout captured."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = radio.cmd_restore(self.conn, argparse.Namespace(handle=handle))
        return rc, buf.getvalue()

    def send_text_calls(self):
        """The pane send-text calls herdr received."""
        return [call for call in self.calls if call[:2] == ("pane", "send-text")]

    def add_account_with_session(self, session_id="sess-1"):
        """A codex account whose home carries the recorded session, so restore
        has something real to resume."""
        home = radio.STATE_DIR / "codex-personal"
        session_file = home / "sessions" / "2026" / "09" / f"rollout-x-{session_id}.jsonl"
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.write_text("{}\n")
        self.conn.execute(
            "INSERT INTO accounts(name, provider, home, created_at) "
            "VALUES ('personal', 'codex', ?, 't')",
            (str(home),),
        )
        self.conn.execute("UPDATE handles SET account='personal' WHERE name='coder'")
        self.conn.commit()
        return home

    def test_types_the_resume_command_into_the_pane(self):
        """Restore types the join --resume command into the pane and presses enter."""
        self.add_account_with_session()
        self.panes["w2:p1"] = {"label": "coder", "workspace_id": "w2"}
        rc, out = self.restore()
        self.assertEqual(rc, 0)
        calls = self.send_text_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], "w2:p1")
        self.assertIn("radio join coder --provider codex --resume", calls[0][3])
        self.assertIn(("pane", "send-keys", "w2:p1", "enter"), self.calls)
        self.assertIn("restoring coder", out)

    def test_restore_starts_fresh_when_the_session_is_missing(self):
        """A session absent from the account home starts fresh."""
        self.conn.execute(
            "INSERT INTO accounts(name, provider, home, created_at) "
            "VALUES ('personal', 'codex', ?, 't')",
            (str(radio.STATE_DIR / "codex-personal"),),
        )
        self.conn.execute("UPDATE handles SET account='personal' WHERE name='coder'")
        self.conn.commit()
        self.panes["w2:p1"] = {"label": "coder", "workspace_id": "w2"}
        rc, out = self.restore()
        self.assertEqual(rc, 0)
        self.assertIn("starting fresh", out)
        self.assertNotIn("--resume", self.send_text_calls()[0][3])

    def test_restore_refuses_when_the_pane_is_busy(self):
        """A busy pane is refused rather than interrupted."""
        self.panes["w2:p1"] = {
            "label": "coder", "workspace_id": "w2", "agent_status": "blocked"
        }
        with self.assertRaises(SystemExit) as ctx:
            self.restore()
        self.assertIn("busy", str(ctx.exception))

    def test_starts_fresh_without_a_recorded_session(self):
        """No recorded session starts fresh with a note."""
        self.conn.execute("UPDATE handles SET agent_session=NULL WHERE name='coder'")
        self.conn.commit()
        self.panes["w2:p1"] = {"label": "coder", "workspace_id": "w2"}
        rc, out = self.restore()
        self.assertEqual(rc, 0)
        self.assertNotIn("--resume", self.send_text_calls()[0][3])
        self.assertIn("no recorded session", out)

    def test_already_running_is_a_noop(self):
        """A pane already running the right agent is left alone."""
        self.panes["w2:p1"] = {"label": "coder", "workspace_id": "w2", "agent": "codex"}
        rc, out = self.restore()
        self.assertEqual(rc, 0)
        self.assertIn("already running", out)
        self.assertEqual(self.send_text_calls(), [])

    def test_missing_pane_reports_what_to_do(self):
        """A missing pane tells the user to run radio join."""
        with self.assertRaises(SystemExit) as ctx:
            self.restore()
        self.assertIn("radio join coder", str(ctx.exception))

    def test_foreign_agent_blocks(self):
        """A pane running a different agent is refused."""
        self.panes["w2:p1"] = {"label": "coder", "workspace_id": "w2", "agent": "claude"}
        with self.assertRaises(SystemExit):
            self.restore()

    def test_handle_without_provider(self):
        """A handle with no provider cannot be restored."""
        self.add_handle("plain", ref="herdr:w2:p2", workspace="w2")
        with self.assertRaises(SystemExit):
            self.restore("plain")

    def test_restore_reports_the_recorded_account(self):
        """The restore output names the account it will use."""
        self.conn.execute("UPDATE handles SET account='codex2' WHERE name='coder'")
        self.conn.execute(
            "INSERT INTO accounts(name, provider, home, created_at) "
            "VALUES ('codex2', 'codex', '/tmp/codex2', 't')"
        )
        self.conn.commit()
        self.panes["w2:p1"] = {"label": "coder", "workspace_id": "w2"}
        rc, out = self.restore()
        self.assertEqual(rc, 0)
        self.assertIn("account: codex2", out)

    def test_restore_reports_a_removed_account(self):
        """A handle pointing at a deleted account exits with an explanation."""
        self.conn.execute("UPDATE handles SET account='codex2' WHERE name='coder'")
        self.conn.commit()
        with self.assertRaises(SystemExit) as ctx:
            self.restore()
        self.assertIn("no longer defined", str(ctx.exception))


class ScopedRosterTest(RadioTestCase):
    """The roster and log are workspace-scoped inside a pane; an explicit
    workspace narrows them from outside."""

    LABELS = {
        "result": {
            "workspaces": [
                {"workspace_id": "w1", "label": "BoilerRoom"},
                {"workspace_id": "w2", "label": "Servers"},
            ]
        }
    }

    def setUp(self):
        """Pretend the command runs in w2 and stub the workspace labels."""
        super().setUp()
        saved_ws = radio.current_workspace
        self.addCleanup(setattr, radio, "current_workspace", saved_ws)
        radio.current_workspace = lambda: "w2"
        saved_fetch = radio.fetch_pane
        self.addCleanup(setattr, radio, "fetch_pane", saved_fetch)
        radio.fetch_pane = lambda pane_id: None
        saved_json = radio.herdr_json
        self.addCleanup(setattr, radio, "herdr_json", saved_json)
        radio.herdr_json = lambda *args: self.LABELS

    def capture(self, fn, *args):
        """Run a command with stdout captured."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(self.conn, *args)
        return buf.getvalue()

    def test_roster_shows_only_own_workspace(self):
        """The roster lists this workspace's handles and its label only."""
        self.add_handle("alice", workspace="w2")
        self.add_handle("hede", workspace="w1")
        out = self.capture(radio.cmd_handles, argparse.Namespace(workspace=None))
        self.assertIn("alice", out)
        self.assertNotIn("hede", out)
        self.assertIn("Servers", out)

    def test_roster_filter_by_label(self):
        """An explicit label narrows the roster to that workspace."""
        self.add_handle("alice", workspace="w2")
        self.add_handle("hede", workspace="w1")
        out = self.capture(radio.cmd_handles, argparse.Namespace(workspace="BoilerRoom"))
        self.assertIn("hede", out)
        self.assertNotIn("alice", out)

    def test_log_is_scoped(self):
        """The log shows only messages that belong to this workspace."""
        self.add_handle("alice", workspace="w2")
        self.add_handle("hede", workspace="w1")
        self.pm("alice", "alice", "own message", from_ws="w2", to_ws="w2")
        self.pm("hede", "hede", "other message", from_ws="w1", to_ws="w1")
        out = self.capture(radio.cmd_log, argparse.Namespace(limit=20))
        self.assertIn("own message", out)
        self.assertNotIn("other message", out)

    def test_show_is_scoped_and_legacy_rows_stay_reachable(self):
        """show refuses another workspace's message; pre-scope rows still read."""
        self.add_handle("hede", workspace="w1")
        foreign = self.pm("hede", "hede", "foreign body", from_ws="w1", to_ws="w1")
        with self.assertRaises(SystemExit):
            self.capture(radio.cmd_show, argparse.Namespace(message_id=foreign, by=None))
        legacy = self.pm("alice", "alice", "legacy body", from_ws="", to_ws="")
        self.assertIn("legacy body", self.capture(
            radio.cmd_show, argparse.Namespace(message_id=legacy, by=None)
        ))


class AccountsMigrationTest(unittest.TestCase):
    """An accounts table from the first cut stored a single `dir`; the
    migration renames it to `home` and adds the env column."""

    def test_old_accounts_shape_converges(self):
        """A dir-shaped accounts table is renamed to home with an empty env."""
        tmp = Path(tempfile.mkdtemp(prefix="radio-accounts-"))
        db = tmp / "radio.db"
        legacy = sqlite3.connect(db)
        legacy.executescript(
            """
            CREATE TABLE accounts(
              name TEXT PRIMARY KEY,
              provider TEXT NOT NULL,
              dir TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            INSERT INTO accounts(name, provider, dir, created_at)
              VALUES ('codex2', 'codex', 'C:/x/.codex-account-2', 't');
            """
        )
        legacy.commit()
        legacy.close()
        saved = (radio.STATE_DIR, radio.DB_PATH, radio.LOCK_PATH)
        self.addCleanup(self._restore, saved)
        radio.STATE_DIR = tmp
        radio.DB_PATH = db
        radio.LOCK_PATH = tmp / "relay.lock"
        conn = radio.connect()
        self.addCleanup(conn.close)
        row = conn.execute("SELECT * FROM accounts WHERE name='codex2'").fetchone()
        self.assertEqual(row["home"], "C:/x/.codex-account-2")
        self.assertEqual(row["env"], "{}")

    @staticmethod
    def _restore(saved):
        """Put radio's module paths back."""
        radio.STATE_DIR, radio.DB_PATH, radio.LOCK_PATH = saved


class ScopeMigrationTest(unittest.TestCase):
    """A pre-scope ledger migrates to the scoped schema: rows survive as the
    unscoped namespace, the new columns exist, and the same name can then be
    joined again in another workspace."""

    def setUp(self):
        """Build a pre-scope ledger with one handle, message and delivery."""
        self.tmp = Path(tempfile.mkdtemp(prefix="radio-legacy-"))
        legacy = sqlite3.connect(self.tmp / "radio.db")
        legacy.executescript(
            """
            CREATE TABLE handles(
              name TEXT PRIMARY KEY,
              session_ref TEXT NOT NULL DEFAULT 'manual',
              kind TEXT NOT NULL DEFAULT 'terminal',
              agent TEXT,
              agent_session TEXT,
              briefed_at TEXT,
              created_at TEXT NOT NULL,
              last_seen TEXT NOT NULL
            );
            CREATE TABLE messages(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL, kind TEXT NOT NULL,
              from_handle TEXT NOT NULL, to_handle TEXT, text TEXT NOT NULL,
              ref TEXT, reply_required INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE deliveries(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              message_id INTEGER NOT NULL,
              target TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              attempts INTEGER NOT NULL DEFAULT 0,
              last_error TEXT, created_at TEXT NOT NULL,
              delivered_at TEXT, last_attempt_at TEXT
            );
            INSERT INTO handles(name, session_ref, agent, created_at, last_seen)
              VALUES ('bob', 'herdr:w1:p1', 'claude', '2026-01-01', '2026-01-01');
            INSERT INTO messages(ts, kind, from_handle, to_handle, text)
              VALUES ('2026-01-01', 'pm', 'alice', 'bob', 'hi');
            INSERT INTO deliveries(message_id, target, created_at)
              VALUES (1, 'bob', '2026-01-01');
            """
        )
        legacy.commit()
        legacy.close()
        saved = (radio.STATE_DIR, radio.DB_PATH, radio.LOCK_PATH)
        self.addCleanup(self._restore, saved)
        radio.STATE_DIR = self.tmp
        radio.DB_PATH = self.tmp / "radio.db"
        radio.LOCK_PATH = self.tmp / "relay.lock"

    @staticmethod
    def _restore(saved):
        """Put radio's module paths back."""
        radio.STATE_DIR, radio.DB_PATH, radio.LOCK_PATH = saved

    def test_migration_preserves_rows_and_adds_scope(self):
        """Rows survive as unscoped, the scope columns exist, and the same name can join again."""
        conn = radio.connect()
        self.addCleanup(conn.close)
        row = conn.execute("SELECT * FROM handles").fetchone()
        self.assertEqual((row["workspace"], row["name"], row["agent"]), ("", "bob", "claude"))
        self.assertIsNone(row["role"])
        message = conn.execute("SELECT * FROM messages").fetchone()
        self.assertEqual((message["from_ws"], message["to_ws"]), ("", ""))
        delivery = conn.execute("SELECT * FROM deliveries").fetchone()
        self.assertEqual(delivery["target_ws"], "")
        conn.execute(
            "INSERT INTO handles(workspace, name, created_at, last_seen) "
            "VALUES ('w2', 'bob', 't', 't')"
        )
        conn.commit()
        count = conn.execute("SELECT COUNT(*) AS c FROM handles").fetchone()["c"]
        self.assertEqual(count, 2)


class CatchUpCompactionTest(RadioTestCase):
    """cmd_join's catch-up: on a pane rejoin only the newest reply-required PM per sender
    stays push-worthy; the rest are left for pull."""

    def setUp(self):
        """Capture the pane functions the join path uses."""
        super().setUp()
        self._fetch = radio.fetch_pane
        self._pane_exists = radio.pane_exists
        self.addCleanup(self._restore)

    def _restore(self):
        """Put the stubbed pane functions back."""
        radio.fetch_pane = self._fetch
        radio.pane_exists = self._pane_exists

    def seed_backlog(self):
        """Seed a mixed backlog of plain and reply-required PMs plus one failed delivery."""
        self.add_handle("bob", ref="herdr:w1:p1")
        m1 = self.pm("alice", "bob", "plain 1")
        m2 = self.pm("alice", "bob", "rr old", reply_required=True)
        m3 = self.pm("alice", "bob", "rr new", reply_required=True)
        m4 = self.pm("carol", "bob", "plain 2")
        m5 = self.pm("carol", "bob", "rr carol", reply_required=True)
        m6 = self.pm("alice", "bob", "gave up")
        self.conn.execute("UPDATE deliveries SET status='failed' WHERE message_id=?",
                          (m6,))
        self.conn.commit()
        return m1, m2, m3, m4, m5, m6

    def statuses(self):
        """Map message id -> (status, last_error) for every delivery."""
        return {
            r["message_id"]: (r["status"], r["last_error"])
            for r in self.conn.execute("SELECT * FROM deliveries").fetchall()
        }

    def test_pane_rejoin_compacts_backlog(self):
        """A pane rejoin keeps the newest reply-required mail pending and moves the rest,
        including failed rows, to pull."""
        m1, m2, m3, m4, m5, m6 = self.seed_backlog()
        radio.fetch_pane = lambda pane_id: {"label": "bob"}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            radio.cmd_join(self.conn, join_args("bob", pane="w1:p1"))
        got = self.statuses()
        # Only the newest reply-required PM per sender stays push-worthy.
        self.assertEqual(got[m3][0], "pending")
        self.assertEqual(got[m5][0], "pending")
        for mid in (m1, m2, m4):
            self.assertEqual(got[mid], ("pull", "catch-up: read via radio inbox"))
        # A terminal 'failed' delivery becomes fetchable again, never pushed.
        self.assertEqual(got[m6], ("pull", "catch-up: read via radio inbox"))
        self.assertIn("catch-up: 2 push (reply-required), 4 left for pull", buf.getvalue())

    def test_manual_join_compacts_nothing(self):
        """A manual join (no pane) leaves the backlog untouched."""
        m1, m2, m3, m4, m5, m6 = self.seed_backlog()
        radio.pane_exists = lambda pane_id: False  # old pane gone: ref stays manual
        with contextlib.redirect_stdout(io.StringIO()):
            radio.cmd_join(self.conn, join_args("bob"))
        got = self.statuses()
        for mid in (m1, m2, m3, m4, m5):
            self.assertEqual(got[mid][0], "pending")
        self.assertEqual(got[m6][0], "failed")


class RepromotionTest(RadioTestCase):
    """The relay re-promotes a delivery only when its pull reason was transient (no live pane)."""

    def setUp(self):
        """Seed bob on a pane and capture pushes."""
        super().setUp()
        self.add_handle("bob", ref="herdr:w1:p1")
        self._fetch = radio.fetch_pane
        self._push = radio.push_to_pane
        self.addCleanup(self._restore)
        self.push_calls = []

    def _restore(self):
        """Put the stubbed pane functions back."""
        radio.fetch_pane = self._fetch
        radio.push_to_pane = self._push

    def stranded(self, last_error):
        """Create a pull delivery with the given last_error."""
        mid = self.pm("alice", "bob", "hi")
        self.conn.execute("UPDATE deliveries SET status='pull', last_error=? "
                          "WHERE message_id=?", (last_error, mid))
        self.conn.commit()
        return mid

    def tick(self):
        """Run one relay tick against a live bob pane."""
        radio.fetch_pane = lambda pane_id: {"label": "bob"}

        def push(pane_id, text):
            """Push stub that records the pane id and reports success."""
            self.push_calls.append(pane_id)
            return True, None

        radio.push_to_pane = push
        return radio.relay_tick(self.conn)

    def test_no_live_pane_is_repromoted_and_pushed(self):
        """A delivery stranded on 'no live pane' is pushed when the pane is back."""
        mid = self.stranded("no live pane")
        self.tick()
        self.assertEqual(self.push_calls, ["w1:p1"])
        self.assertEqual(self.delivery_row(mid)["status"], "delivered")

    def test_deliberate_pull_stays_pull(self):
        """Catch-up and unconfirmed pulls are never re-promoted."""
        m1 = self.stranded("catch-up: read via radio inbox")
        m2 = self.stranded("delivery unconfirmed — left for pull, not retried")
        self.tick()
        self.assertEqual(self.push_calls, [])
        self.assertEqual(self.delivery_row(m1)["status"], "pull")
        self.assertEqual(self.delivery_row(m2)["status"], "pull")


class RetryBackoffTest(RadioTestCase):
    """Retry selection: fresh mail first, errored mail after RETRY_AFTER_S, and no stamp on
    a defer."""

    def setUp(self):
        """Seed bob on a pane and capture pushes."""
        super().setUp()
        self.add_handle("bob", ref="herdr:w1:p1")
        self._fetch = radio.fetch_pane
        self._push = radio.push_to_pane
        self.addCleanup(self._restore)
        self.push_calls = []

    def _restore(self):
        """Put the stubbed pane functions back."""
        radio.fetch_pane = self._fetch
        radio.push_to_pane = self._push

    def tick(self, push_result=(True, None)):
        """Run one relay tick with the given push result."""
        radio.fetch_pane = lambda pane_id: {"label": "bob"}

        def push(pane_id, text):
            """Push stub that records the envelope and returns the configured result."""
            self.push_calls.append(text)
            return push_result

        radio.push_to_pane = push
        return radio.relay_tick(self.conn)

    def test_errored_delivery_backs_off_then_retries(self):
        """An errored delivery is not retried inside the window and is retried after it."""
        mid = self.pm("alice", "bob", "hi")
        self.tick((False, "wedged"))
        row = self.delivery_row(mid)
        self.assertEqual(row["attempts"], 1)
        self.assertIsNotNone(row["last_attempt_at"])
        self.assertEqual(len(self.push_calls), 1)
        # Inside RETRY_AFTER_S the errored delivery is not selected again.
        self.tick((False, "wedged"))
        self.assertEqual(len(self.push_calls), 1)
        # Back-dated past the window it becomes due and is retried.
        self.backdate_attempt(mid, radio.RETRY_AFTER_S + 10)
        self.tick((False, "wedged"))
        self.assertEqual(len(self.push_calls), 2)

    def test_fresh_delivery_is_always_selected(self):
        """A fresh delivery is selected immediately and stamped on success."""
        mid = self.pm("alice", "bob", "hi")
        self.tick()
        row = self.delivery_row(mid)
        self.assertEqual(row["status"], "delivered")
        self.assertIsNotNone(row["last_attempt_at"])  # attempts stamp on success too
        self.assertEqual(len(self.push_calls), 1)

    def test_fresh_first_ordering(self):
        """New mail is pushed before a due retry."""
        m1 = self.pm("alice", "bob", "old, errored once")
        self.tick((False, "wedged"))
        self.backdate_attempt(m1, radio.RETRY_AFTER_S + 10)  # due for retry
        m2 = self.pm("alice", "bob", "new mail")
        self.push_calls.clear()
        self.tick()
        self.assertEqual(len(self.push_calls), 2)
        self.assertIn(f"id={m2}", self.push_calls[0])  # fresh mail goes first
        self.assertIn(f"id={m1}", self.push_calls[1])

    def test_agent_blocked_defer_stamps_nothing(self):
        """An agent-blocked defer consumes no attempt and no timestamp."""
        mid = self.pm("alice", "bob", "hi")
        self.tick((False, "agent_blocked"))
        row = self.delivery_row(mid)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)
        self.assertIsNone(row["last_attempt_at"])


class CompareAndSwapTest(RadioTestCase):
    """The relay's status update is a compare-and-swap, so a concurrent ack wins."""

    def test_ack_during_push_is_not_overwritten(self):
        """An ack landing between the relay's select and update is not clobbered."""
        self.add_handle("bob", ref="herdr:w1:p1")
        mid = self.pm("alice", "bob", "hi")
        delivery_id = self.delivery_row(mid)["id"]
        orig_fetch = radio.fetch_pane
        self.addCleanup(setattr, radio, "fetch_pane", orig_fetch)
        radio.fetch_pane = lambda pane_id: {"label": "bob"}
        orig_push = radio.push_to_pane
        self.addCleanup(setattr, radio, "push_to_pane", orig_push)

        def push(pane_id, text):
            # A cmd_show-style ack lands between the relay's SELECT and UPDATE.
            """Push stub that lands a cmd_show-style ack before returning success."""
            self.conn.execute(
                "UPDATE deliveries SET status='delivered', delivered_at='ack-ts' "
                "WHERE id = ? AND status IN ('pending', 'pull', 'failed')",
                (delivery_id,),
            )
            return True, None

        radio.push_to_pane = push
        events = radio.relay_tick(self.conn)
        row = self.delivery_row(mid)
        # The CAS update must not clobber the ack or claim the delivery.
        self.assertEqual(row["status"], "delivered")
        self.assertEqual(row["delivered_at"], "ack-ts")
        self.assertFalse(any(e.startswith(f"#{delivery_id} ") for e in events))


class ChangedEventsTest(RadioTestCase):
    """changed_events suppresses repeated delivery lines but always prints other events."""

    def test_fingerprinting(self):
        """Repeated delivery text prints once; changed text and non-delivery events always print."""
        seen = {}
        waiting = "#3 -> bob (w1:p1) waiting: agent blocked"
        self.assertEqual(radio.changed_events([waiting], seen), [waiting])
        self.assertEqual(radio.changed_events([waiting], seen), [])  # repeat suppressed
        moved = "#3 -> bob (w1:p1) delivered"
        self.assertEqual(radio.changed_events([moved], seen), [moved])  # changed text prints
        batch = ["tick done", "#4 -> bob: no live pane, left for pull"]
        self.assertEqual(radio.changed_events(batch, seen), batch)
        # Non-delivery events print every time.
        self.assertEqual(radio.changed_events(["tick done"], seen), ["tick done"])


class HerdrConsoleTest(unittest.TestCase):
    """Captured Herdr calls must not open a console window on Windows."""

    def test_background_calls_capture_output_without_a_windows_console(self):
        """CREATE_NO_WINDOW is passed on Windows and 0 on POSIX."""
        for platform, flags in (("nt", 0x08000000), ("posix", 0)):
            with self.subTest(platform=platform), \
                    patch.object(radio, "os", types.SimpleNamespace(name=platform)), \
                    patch.object(radio, "herdr_bin", return_value="herdr"), \
                    patch.object(radio.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                    patch.object(radio.subprocess, "run") as run:
                result = radio.herdr("pane", "get", "w1:p1", timeout=3)
                run.assert_called_once_with(
                    ["herdr", "pane", "get", "w1:p1"],
                    text=True, capture_output=True, timeout=3,
                    encoding="utf-8", errors="replace", creationflags=flags,
                )
                self.assertIs(result, run.return_value)


class TimeoutSplitTest(RadioTestCase):
    """Read-only herdr calls use the short timeout; real sends use the longer one."""

    def setUp(self):
        """Capture the subprocess module and the timeouts passed."""
        super().setUp()
        self._subprocess = radio.subprocess
        self.addCleanup(self._restore)
        self.timeouts = []

    def _restore(self):
        """Put the real subprocess module back."""
        radio.subprocess = self._subprocess

    def install_stub(self, returncode=0, stdout="{}"):
        """Install a subprocess stub that records each call's timeout."""
        import subprocess as real_subprocess
        timeouts = self.timeouts

        class Stub:
            """subprocess stand-in that records each call's timeout."""

            TimeoutExpired = real_subprocess.TimeoutExpired
            CompletedProcess = real_subprocess.CompletedProcess
            CREATE_NO_WINDOW = getattr(real_subprocess, "CREATE_NO_WINDOW", 0x08000000)

            @staticmethod
            def run(cmd, **kwargs):
                """Record the timeout and return a completed process."""
                timeouts.append(kwargs.get("timeout"))
                return real_subprocess.CompletedProcess(
                    cmd, returncode, stdout=stdout, stderr=""
                )

        radio.subprocess = Stub

    def test_read_path_uses_read_timeout(self):
        """fetch_pane passes HERDR_READ_TIMEOUT."""
        self.install_stub(stdout='{"result": {"pane": {"label": "x"}}}')
        pane = radio.fetch_pane("w1:p1")
        self.assertEqual(pane, {"label": "x"})
        self.assertEqual(self.timeouts, [radio.HERDR_READ_TIMEOUT])

    def test_send_path_uses_send_timeout(self):
        """push_to_pane passes HERDR_SEND_TIMEOUT."""
        self.install_stub(returncode=0)
        ok, error = radio.push_to_pane("w1:p1", "hi")
        self.assertEqual((ok, error), (True, None))
        self.assertEqual(self.timeouts, [radio.HERDR_SEND_TIMEOUT])


class Utf8StreamsTest(RadioTestCase):
    """Windows pipes default to cp1252; radio forces UTF-8 so typography in
    its output (·, ⚠, —) can never crash a command."""

    def test_reconfigures_real_streams_and_ignores_replaced_ones(self):
        """A cp1252 stream is reconfigured to UTF-8; a replaced StringIO is left alone."""
        buf = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        saved = sys.stdout
        self.addCleanup(setattr, sys, "stdout", saved)
        sys.stdout = buf
        radio.utf8_streams()
        self.assertEqual(buf.encoding.lower(), "utf-8")
        sys.stdout = io.StringIO()  # replaced stream: must not raise
        radio.utf8_streams()


class RelayCwdTest(RadioTestCase):
    """The relay must leave the plugin directory: a daemon cwd inside the
    managed plugin dir blocks Herdr's install/update on Windows."""

    def test_relay_chdirs_to_the_state_dir(self):
        """The relay chdirs to the state dir before its first tick."""
        calls = []
        saved_chdir = radio.os.chdir
        saved_tick = radio.relay_tick
        self.addCleanup(setattr, radio.os, "chdir", saved_chdir)
        self.addCleanup(setattr, radio, "relay_tick", saved_tick)
        radio.os.chdir = lambda path: calls.append(path)

        def stop(_conn):
            """Tick stub that ends the relay loop with KeyboardInterrupt."""
            raise KeyboardInterrupt

        radio.relay_tick = stop
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                radio.cmd_relay(argparse.Namespace(interval=0.01))
        self.assertEqual(calls, [radio.STATE_DIR])


class RelayUpdateTest(RadioTestCase):
    """The relay survives an in-place update: a missing or mid-replacement
    script must not kill the daemon, and an on-disk change hands over to a
    detached successor instead of a fragile re-exec."""

    def test_script_changed_is_false_when_unreadable(self):
        """An unreadable script reports no change; a real one reports a change."""
        self.assertFalse(radio.script_changed("/nonexistent/radio", 1.0))
        self.assertTrue(radio.script_changed(str(REPO / "bin" / "radio"), 1.0))

    def test_partial_writes_are_not_handed_over(self):
        """A script with a syntax error is rejected before handover."""
        self.assertFalse(radio.script_compiles("/nonexistent/radio"))
        broken = radio.STATE_DIR / "broken-radio"
        broken.write_text("def broken(:\n", encoding="utf-8")
        self.assertFalse(radio.script_compiles(str(broken)))
        self.assertTrue(radio.script_compiles(str(REPO / "bin" / "radio")))

    def test_mid_write_keeps_the_old_code_running(self):
        """A broken on-disk script keeps the loop running with no successor spawned."""
        saved = (
            radio.script_changed,
            radio.script_compiles,
            radio.relay_tick,
            radio.time.sleep,
            radio.subprocess.Popen,
        )
        self.addCleanup(self._restore_parts, saved)
        radio.script_changed = lambda script, mtime: True
        radio.script_compiles = lambda script: False
        spawned = []
        radio.subprocess.Popen = lambda *args, **kwargs: spawned.append(args)
        calls = []

        def tick(_conn):
            """Tick stub that keeps the loop alive for two ticks, then stops it."""
            calls.append(1)
            if len(calls) >= 2:
                raise KeyboardInterrupt
            return []

        radio.relay_tick = tick
        radio.time.sleep = lambda _seconds: None
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                radio.cmd_relay(argparse.Namespace(interval=0.01))
        self.assertEqual(spawned, [])
        self.assertEqual(len(calls), 2)

    @staticmethod
    def _restore_parts(saved):
        """Put the five stubbed parts back."""
        (
            radio.script_changed,
            radio.script_compiles,
            radio.relay_tick,
            radio.time.sleep,
            radio.subprocess.Popen,
        ) = saved

    def test_on_disk_change_hands_over_to_a_successor(self):
        """A valid change spawns the detached successor with the lock released."""
        saved = (radio.script_changed, radio.subprocess.Popen, radio.relay_tick, radio.time.sleep)
        self.addCleanup(self._restore, saved)
        radio.script_changed = lambda script, mtime: True
        spawned = []

        class FakePopen:
            """Popen stand-in that records the spawned successor."""

            def __init__(self, argv, **kwargs):
                """Record the argv and kwargs of the spawn."""
                spawned.append((argv, kwargs))

        radio.subprocess.Popen = FakePopen
        radio.relay_tick = lambda _conn: []
        radio.time.sleep = lambda _seconds: None
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = radio.cmd_relay(argparse.Namespace(interval=0.01))
        self.assertEqual(rc, 0)
        self.assertIn("restarting relay", buf.getvalue())
        self.assertEqual(len(spawned), 1)
        argv, kwargs = spawned[0]
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1], str((REPO / "bin" / "radio").resolve()))
        self.assertEqual(argv[2:4], ["relay", "--interval"])
        self.assertEqual(kwargs["cwd"], str(radio.STATE_DIR))
        # The lock was released before the successor starts.
        probe = open(radio.LOCK_PATH, "a+")
        self.addCleanup(probe.close)
        self.assertTrue(radio.relay_lock(probe))

    @staticmethod
    def _restore(saved):
        """Put the stubbed parts back."""
        radio.script_changed, radio.subprocess.Popen, radio.relay_tick, radio.time.sleep = saved


class CrashSafeRelayTest(RadioTestCase):
    """A tick error is logged and the daemon keeps running."""

    def test_tick_error_is_logged_and_the_daemon_survives(self):
        """An injected RuntimeError is logged; the loop continues until the KeyboardInterrupt."""
        orig_tick = radio.relay_tick
        self.addCleanup(setattr, radio, "relay_tick", orig_tick)
        calls = []

        def flaky_tick(conn):
            """Tick stub that raises once, then stops the loop."""
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            raise KeyboardInterrupt  # the only way out of the daemon loop

        radio.relay_tick = flaky_tick
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(KeyboardInterrupt):
                radio.cmd_relay(argparse.Namespace(interval=0.01))
        out = buf.getvalue()
        # The injected RuntimeError was logged, not propagated; the loop kept
        # going until the KeyboardInterrupt (not caught by `except Exception`).
        self.assertIn("relay tick error: boom", out)
        self.assertEqual(len(calls), 2)


def load_bin_script(name: str, filename: str):
    """Import one of bin/'s launcher scripts as a module (same loader pattern
    the suite uses for bin/radio and bin/radio-view)."""
    path = REPO / "bin" / filename
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_file_location(name, path, loader=loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ChoosePlainTest(unittest.TestCase):
    """The line-based picker: Windows terminals have no termios, so choose()
    falls back to numbered options read from stdin."""

    def run_pick(self, keys, default=0):
        """Run choose_plain with stdin fed from a string."""
        buf = io.StringIO()
        saved = sys.stdin
        sys.stdin = io.StringIO(keys)
        try:
            with contextlib.redirect_stdout(buf):
                idx = radio.choose_plain("pick", ["a", "b", "c"], default)
        finally:
            sys.stdin = saved
        return idx, buf.getvalue()

    def test_number_selects(self):
        """A number selects that option."""
        self.assertEqual(self.run_pick("2\n")[0], 1)

    def test_enter_takes_the_default(self):
        """Enter returns the default index."""
        self.assertEqual(self.run_pick("\n", default=2)[0], 2)

    def test_q_cancels(self):
        """q returns None."""
        self.assertEqual(self.run_pick("q\n")[0], None)

    def test_closed_stdin_cancels(self):
        """EOF returns None."""
        self.assertEqual(self.run_pick("")[0], None)

    def test_invalid_line_reprompts(self):
        """An invalid line re-prompts and the next valid one wins."""
        idx, out = self.run_pick("nope\n3\n")
        self.assertEqual(idx, 2)
        self.assertIn("invalid choice", out)

    def test_choose_dispatches_when_termios_is_missing(self):
        """choose falls back to the plain picker when termios is unavailable."""
        saved = radio.termios
        self.addCleanup(setattr, radio, "termios", saved)
        radio.termios = None
        saved_stdin = sys.stdin
        sys.stdin = io.StringIO("1\n")
        self.addCleanup(setattr, sys, "stdin", saved_stdin)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(radio.choose("pick", ["a", "b"]), 0)


class RelayLockTest(RadioTestCase):
    """relay_lock: POSIX flock path, plus the Windows msvcrt fallback exercised
    with a fake module (the suite itself runs on POSIX)."""

    def _force_windows_lock(self, fake):
        """Swap in a fake msvcrt so the Windows branch runs on POSIX."""
        saved = radio.fcntl, radio.msvcrt
        self.addCleanup(self._restore, saved)
        radio.fcntl, radio.msvcrt = None, fake

    @staticmethod
    def _restore(saved):
        """Put the real modules back."""
        radio.fcntl, radio.msvcrt = saved

    def test_posix_second_lock_is_refused(self):
        """The second lock attempt on the same file is refused."""
        lock = radio.STATE_DIR / "relay.lock"
        first, second = open(lock, "a+"), open(lock, "a+")
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        self.assertTrue(radio.relay_lock(first))
        self.assertFalse(radio.relay_lock(second))

    def test_windows_fallback_locks_one_byte(self):
        """The Windows fallback locks one byte and keeps the region non-empty."""
        calls = []

        class FakeMsvcrt:
            """msvcrt stand-in whose locking succeeds and records the call."""

            LK_NBLCK = 2

            @staticmethod
            def locking(fd, mode, size):
                """Record the mode and size of the byte-range lock."""
                calls.append((mode, size))

        self._force_windows_lock(FakeMsvcrt)
        with open(radio.STATE_DIR / "relay.lock", "a+") as fd:
            self.assertTrue(radio.relay_lock(fd))
            self.assertEqual(calls, [(FakeMsvcrt.LK_NBLCK, 1)])
            fd.seek(0)
            self.assertEqual(fd.read(1), "1")  # region kept non-empty

    def test_windows_fallback_reports_contention(self):
        """An OSError from msvcrt reports the lock as held."""
        class BusyMsvcrt:
            """msvcrt stand-in whose lock is already held."""

            LK_NBLCK = 2

            @staticmethod
            def locking(fd, mode, size):
                """Report the held lock as an OSError."""
                raise OSError("lock held")

        self._force_windows_lock(BusyMsvcrt)
        with open(radio.STATE_DIR / "relay.lock", "a+") as fd:
            self.assertFalse(radio.relay_lock(fd))


class AgentLaunchArgvTest(unittest.TestCase):
    """agent_launch_argv: POSIX execs the resolved binary; Windows npm-style
    .cmd/.bat shims cannot be CreateProcess'd and get wrapped in cmd /c."""

    def setUp(self):
        """Capture shutil.which and os.name."""
        self._which = radio.shutil.which
        self._name = radio.os.name
        self.addCleanup(self._restore)

    def _restore(self):
        """Put shutil.which and os.name back."""
        radio.shutil.which = self._which
        radio.os.name = self._name

    def test_posix_resolves_the_binary(self):
        """On POSIX the resolved binary path replaces the command name."""
        radio.shutil.which = lambda cmd: f"/usr/local/bin/{cmd}"
        self.assertEqual(
            radio.agent_launch_argv(["claude", "--name", "x"]),
            ["/usr/local/bin/claude", "--name", "x"],
        )

    def test_windows_cmd_shim_is_wrapped(self):
        """A .cmd shim is wrapped in cmd.exe /c."""
        radio.os.name = "nt"
        radio.shutil.which = lambda cmd: r"C:\npm\claude.cmd"
        self.assertEqual(
            radio.agent_launch_argv(["claude", "--name", "x"]),
            ["cmd.exe", "/c", r"C:\npm\claude.cmd", "--name", "x"],
        )

    def test_windows_exe_is_not_wrapped(self):
        """A real .exe is launched directly."""
        radio.os.name = "nt"
        radio.shutil.which = lambda cmd: r"C:\tools\codex.exe"
        self.assertEqual(radio.agent_launch_argv(["codex"]), [r"C:\tools\codex.exe"])


class ExecOrWaitTest(unittest.TestCase):
    """exec_or_wait: POSIX execs in place; Windows waits on a child, because
    the CRT's exec emulation returns the shell prompt while the target runs."""

    def setUp(self):
        """Capture os.name, subprocess.run and os.execvpe."""
        self._name = radio.os.name
        self._run = radio.subprocess.run
        self._execvpe = radio.os.execvpe
        self.addCleanup(self._restore)

    def _restore(self):
        """Put the captured attributes back."""
        radio.os.name = self._name
        radio.subprocess.run = self._run
        radio.os.execvpe = self._execvpe

    def test_posix_execs_in_place(self):
        """On POSIX the target replaces the process via execvpe."""
        seen = {}

        def fake_execvpe(path, argv, env):
            """execvpe stand-in that records the argv and aborts the exec."""
            seen["argv"] = argv
            raise RuntimeError("exec")

        radio.os.execvpe = fake_execvpe
        with self.assertRaises(RuntimeError):
            radio.exec_or_wait(["claude", "--name", "x"], {})
        self.assertEqual(seen["argv"], ["claude", "--name", "x"])

    def test_windows_waits_and_returns_the_exit_code(self):
        """On Windows the child runs and its exit code is returned."""
        radio.os.name = "nt"
        radio.subprocess.run = lambda command, env=None: subprocess.CompletedProcess(command, 3)
        self.assertEqual(radio.exec_or_wait(["cmd.exe", "/c", "claude.cmd"], {}), 3)

    def test_windows_command_line_plain_binary(self):
        """A plain binary path joins without quoting."""
        self.assertEqual(
            radio.windows_command_line([r"C:\tools\codex.exe", "--resume", "sid"]),
            r"C:\tools\codex.exe --resume sid",
        )

    def test_windows_command_line_quotes_space_paths(self):
        # cmd /c strips a lone outer pair; the extra pair keeps the path whole.
        """A path with spaces gets the extra quote pair cmd /c needs."""
        self.assertEqual(
            radio.windows_command_line(
                ["cmd.exe", "/c", r"C:\Program Files\npm\claude.cmd", "--name", "x"]
            ),
            'cmd.exe /c ""C:\\Program Files\\npm\\claude.cmd" --name x"',
        )


class WinShimTest(RadioTestCase):
    """The Windows CLI shim writer (bin/win-shim.py): a stable radio.cmd that
    resolves the plugin root at run time through the cache and resolver, so a
    moved plugin never leaves a dangling path; marker-guarded overwrite (a
    foreign radio.cmd or radio-resolve.py is never clobbered); PATH hint."""

    def setUp(self):
        """Load the shim module and point it at a temp link dir."""
        super().setUp()
        self.win_shim = load_bin_script("radio_win_shim", "win-shim.py")
        self.link_dir = radio.STATE_DIR / "link-bin"

    def test_shim_text_is_stable_and_bakes_no_root(self):
        """The launcher text is marker-guarded and carries no plugin path."""
        text = self.win_shim.shim_text()
        self.assertTrue(text.startswith(self.win_shim.SHIM_MARKER))
        self.assertIn(self.win_shim.ROOT_FILE, text)
        self.assertIn(self.win_shim.RESOLVER_FILE, text)
        self.assertNotIn("plugins\\radio-", text)  # no baked plugin path

    def test_shim_gates_on_python_310(self):
        """Every interpreter probe checks the version, not just that it runs,
        and the check itself uses no cmd-syntax characters."""
        text = self.win_shim.shim_text()
        self.assertIn("version_info", text)
        self.assertNotIn("--version", text)
        self.assertEqual(text.count("RADIO_PYCHECK"), 4)  # one set + three probes
        raw = text.split('set "RADIO_PYCHECK=')[1].split('"')[0]
        # cmd reads ( ) < > as syntax even inside a quoted value.
        self.assertFalse(set("()<>") & set(raw))
        check = raw.removeprefix("import sys; ")
        for major, minor, accepted in ((3, 9, False), (3, 10, True), (3, 12, True),
                                       (3, 99, True), (2, 7, False), (4, 0, False)):
            fake = types.SimpleNamespace(
                version_info=types.SimpleNamespace(major=major, minor=minor)
            )
            with self.subTest(version=f"{major}.{minor}"):
                if accepted:
                    exec(check, {"sys": fake})
                else:
                    with self.assertRaises(AssertionError):
                        exec(check, {"sys": fake})



    def test_resolver_text_queries_herdr(self):
        """The resolver asks herdr for the plugin root."""
        text = self.win_shim.resolver_text()
        self.assertIn(self.win_shim.RESOLVER_MARKER, text)
        self.assertIn("plugin_root", text)
        self.assertIn('"radio"', text)

    def test_ensure_shim_writes_launcher_resolver_and_cache(self):
        """ensure_shim writes the launcher, resolver and root cache; a new root only
        refreshes the cache."""
        msg = self.win_shim.ensure_shim(Path(r"C:\plugins\radio-a"), self.link_dir)
        self.assertIn("radio.cmd ->", msg)
        shim = self.link_dir / "radio.cmd"
        self.assertTrue(shim.exists())
        self.assertTrue((self.link_dir / self.win_shim.RESOLVER_FILE).exists())
        cache = self.link_dir / self.win_shim.ROOT_FILE
        self.assertEqual(cache.read_text(encoding="utf-8").strip(), r"C:\plugins\radio-a")
        # The launcher is stable across updates: a new root only refreshes the
        # cache, so the installed shim never carries a stale path.
        shim_before = shim.read_text(encoding="utf-8")
        self.win_shim.ensure_shim(Path(r"C:\plugins\radio-b"), self.link_dir)
        self.assertEqual(shim.read_text(encoding="utf-8"), shim_before)
        self.assertEqual(cache.read_text(encoding="utf-8").strip(), r"C:\plugins\radio-b")

    def test_a_failed_write_keeps_the_previous_shim(self):
        """A failed write leaves the working shim in place and no temp file."""
        self.win_shim.ensure_shim(Path(r"C:\plugins\radio-a"), self.link_dir)
        shim = self.link_dir / "radio.cmd"
        before = shim.read_text(encoding="utf-8")
        saved = self.win_shim.os.replace
        self.addCleanup(setattr, self.win_shim.os, "replace", saved)

        def locked(src, dst):
            """Fail the replace the way a locked target file does."""
            raise OSError("locked")

        self.win_shim.os.replace = locked
        msg = self.win_shim.ensure_shim(Path(r"C:\plugins\radio-b"), self.link_dir)
        self.assertIn("not written", msg)
        self.assertEqual(shim.read_text(encoding="utf-8"), before)
        self.assertEqual(list(self.link_dir.glob("*.tmp")), [])

    def test_foreign_radio_cmd_is_left_alone(self):
        """A radio.cmd without the marker is never overwritten."""
        self.link_dir.mkdir(parents=True, exist_ok=True)
        shim = self.link_dir / "radio.cmd"
        shim.write_text("@echo off\necho someone else's radio\n", encoding="utf-8")
        msg = self.win_shim.ensure_shim(Path(r"C:\plugins\radio-a"), self.link_dir)
        self.assertIn("left alone", msg)
        self.assertIn("someone else's radio", shim.read_text(encoding="utf-8"))

    def test_append_path_entry_appends_and_dedupes(self):
        """The PATH entry is appended once and trimmed of empty segments."""
        target = r"C:\Users\x\.local\bin"
        self.assertEqual(self.win_shim.append_path_entry("", target), target)
        self.assertEqual(
            self.win_shim.append_path_entry(r"C:\Windows;C:\Tools", target),
            r"C:\Windows;C:\Tools;" + target,
        )
        self.assertIsNone(self.win_shim.append_path_entry(target + r";D:\x", target))
        self.assertEqual(
            self.win_shim.append_path_entry(r"C:\Windows;;", target),
            r"C:\Windows;" + target,
        )

    def test_ensure_user_path_noop_off_windows(self):
        """ensure_user_path does nothing off Windows."""
        self.assertIsNone(self.win_shim.ensure_user_path(self.link_dir))

    def test_foreign_resolver_is_left_alone(self):
        """A resolver without the marker is never overwritten."""
        self.link_dir.mkdir(parents=True, exist_ok=True)
        resolver = self.link_dir / self.win_shim.RESOLVER_FILE
        resolver.write_text("print('someone else')\n", encoding="utf-8")
        msg = self.win_shim.ensure_shim(Path(r"C:\plugins\radio-a"), self.link_dir)
        self.assertIn("left alone", msg)
        self.assertIn("someone else", resolver.read_text(encoding="utf-8"))

    def test_path_hint_only_when_missing(self):
        """The PATH hint appears only when the dir is not on PATH."""
        saved = os.environ.get("PATH")
        self.addCleanup(self._restore_path, saved)
        os.environ["PATH"] = str(self.link_dir)
        self.assertIsNone(self.win_shim.path_hint(self.link_dir))
        os.environ["PATH"] = "/nowhere"
        hint = self.win_shim.path_hint(self.link_dir)
        self.assertIn(str(self.link_dir), hint)

    @staticmethod
    def _restore_path(saved):
        """Put PATH back."""
        if saved is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = saved


class ViewPythonTest(unittest.TestCase):
    """run-view.py: prefer the state-dir venv interpreter (per-platform
    layout), fall back to the launcher's own interpreter."""

    def setUp(self):
        """Load run-view.py as a module."""
        self.run_view = load_bin_script("radio_run_view", "run-view.py")

    def test_prefers_posix_venv(self):
        """The POSIX venv interpreter is preferred when present."""
        with tempfile.TemporaryDirectory() as tmp:
            venv = Path(tmp) / "venv"
            venv_python = venv / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("")
            self.assertEqual(self.run_view.view_python(venv), venv_python)

    def test_prefers_windows_venv(self):
        """The Scripts/python.exe layout is preferred on Windows."""
        with tempfile.TemporaryDirectory() as tmp:
            venv = Path(tmp) / "venv"
            venv_python = venv / "Scripts" / "python.exe"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("")
            # Module-local fake os: patching the real os.name would make
            # pathlib instantiate WindowsPath on this POSIX test host.
            saved = self.run_view.os
            self.addCleanup(setattr, self.run_view, "os", saved)
            self.run_view.os = types.SimpleNamespace(name="nt")
            self.assertEqual(self.run_view.view_python(venv), venv_python)

    def test_falls_back_when_venv_is_missing(self):
        """A missing venv falls back to the launcher's interpreter."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                self.run_view.view_python(Path(tmp) / "venv"), Path(sys.executable)
            )

    def test_state_dir_honours_radio_home(self):
        """RADIO_HOME overrides the state dir."""
        saved = os.environ.get("RADIO_HOME")
        self.addCleanup(self._restore_env, saved)
        os.environ["RADIO_HOME"] = "/tmp/radio-home-test"
        self.assertEqual(self.run_view.state_dir(), Path("/tmp/radio-home-test"))

    def test_detach_cwd_creates_the_state_dir_and_moves_into_it(self):
        """detach_cwd makes the state dir when it is missing, then moves there."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "radio-home"
            saved_home = os.environ.get("RADIO_HOME")
            self.addCleanup(self._restore_env, saved_home)
            os.environ["RADIO_HOME"] = str(home)
            calls = []
            saved_chdir = self.run_view.os.chdir
            self.addCleanup(setattr, self.run_view.os, "chdir", saved_chdir)
            self.run_view.os.chdir = lambda path: calls.append(path)
            self.run_view.detach_cwd()
            self.assertEqual(calls, [home])
            self.assertTrue(home.is_dir())

    @staticmethod
    def _restore_env(saved):
        """Put RADIO_HOME back."""
        if saved is None:
            os.environ.pop("RADIO_HOME", None)
        else:
            os.environ["RADIO_HOME"] = saved


class WorkspaceCreatedHookTest(unittest.TestCase):
    """The workspace.created hook reads the event payload defensively and
    opens the platform-correct Radio view pane without stealing focus."""

    def setUp(self):
        """Load the hook and clear the event env it reads."""
        self.hook = load_bin_script("radio_workspace_hook", "workspace-created.py")
        for key in ("HERDR_PLUGIN_EVENT_JSON", "HERDR_WORKSPACE_ID", "HERDR_BIN_PATH"):
            saved = os.environ.pop(key, None)
            if saved is not None:
                self.addCleanup(os.environ.__setitem__, key, saved)

    def test_event_workspace_variants(self):
        """The known payload shapes yield the workspace id; bad input yields an empty string."""
        self.assertEqual(self.hook.event_workspace('{"workspace":{"workspace_id":"w7"}}'), "w7")
        self.assertEqual(self.hook.event_workspace('{"workspace_id":"wA","x":1}'), "wA")
        self.assertEqual(self.hook.event_workspace('{"items":[{"workspace_id":"w9"}]}'), "w9")
        self.assertEqual(self.hook.event_workspace("not json"), "")
        self.assertEqual(self.hook.event_workspace("{}"), "")

    def test_targeted_keys_win_over_other_workspace_ids(self):
        # A decoy list of workspaces must not steal the event's workspace.
        """The event's own workspace wins over a decoy workspace list."""
        payload = '{"workspace": {"workspace_id": "w7"}, "workspaces": [{"workspace_id": "w1"}]}'
        self.assertEqual(self.hook.event_workspace(payload), "w7")
        self.assertEqual(
            self.hook.event_workspace('{"workspaces": [{"workspace_id": "w1"}]}'), "w1"
        )

    def test_main_opens_the_view_in_the_new_workspace(self):
        """The hook opens the view pane for the event workspace with --no-focus."""
        calls = []

        class FakeSubprocess:
            """subprocess stand-in that records the hook's command."""

            TimeoutExpired = subprocess.TimeoutExpired

            @staticmethod
            def run(argv, **kwargs):
                """Record the argv the hook would run."""
                calls.append(argv)

        saved = self.hook.subprocess
        self.addCleanup(setattr, self.hook, "subprocess", saved)
        self.hook.subprocess = FakeSubprocess
        os.environ["HERDR_BIN_PATH"] = "/opt/herdr"
        os.environ["HERDR_PLUGIN_EVENT_JSON"] = '{"workspace":{"workspace_id":"w7"}}'
        self.hook.main()
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertEqual(argv[:5], ["/opt/herdr", "plugin", "pane", "open", "--plugin"])
        self.assertIn("radio", argv)
        self.assertIn("view", argv)
        self.assertIn("w7", argv)
        self.assertIn("--no-focus", argv)

    def test_main_noops_without_a_workspace(self):
        """No payload means no action."""
        self.hook.main()  # no payload, no env: returns quietly


class WindowsScriptsTest(unittest.TestCase):
    """The Windows hook bodies must import and no-op cleanly off Windows."""

    def test_noops_off_windows(self):
        """setup-win and autostart-win import and return 0 off Windows."""
        for filename in ("setup-win.py", "autostart-win.py"):
            module = load_bin_script(Path(filename).stem.replace("-", "_"), filename)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.main(), 0)


# The view module sys.exit()s at import when textual is missing; guard so the
# suite still runs with a stdlib-only interpreter.
try:
    _vloader = importlib.machinery.SourceFileLoader("radio_view", str(REPO / "bin" / "radio-view"))
    _vspec = importlib.util.spec_from_file_location("radio_view", REPO / "bin" / "radio-view", loader=_vloader)
    radio_view = importlib.util.module_from_spec(_vspec)
    _vspec.loader.exec_module(radio_view)
    HAS_VIEW = True
except SystemExit:
    radio_view = None
    HAS_VIEW = False


@unittest.skipUnless(HAS_VIEW, "view deps (textual) not installed")
class SidebarPolicyTest(unittest.TestCase):
    """Responsive sidebar policy (radio-view): auto-hide below NARROW_COLS,
    manual toggle wins over width."""

    def test_auto_threshold(self):
        # Below the threshold the fixed sidebar would starve the stream.
        """The sidebar hides below NARROW_COLS and shows at or above it."""
        self.assertFalse(radio_view.sidebar_visible(radio_view.NARROW_COLS - 1, None))
        self.assertTrue(radio_view.sidebar_visible(radio_view.NARROW_COLS, None))
        self.assertTrue(radio_view.sidebar_visible(200, None))

    def test_forced_overrides_width(self):
        """An explicit toggle wins over the width."""
        self.assertTrue(radio_view.sidebar_visible(40, True))
        self.assertFalse(radio_view.sidebar_visible(200, False))


@unittest.skipUnless(HAS_VIEW, "view deps (textual) not installed")
class CompactRosterTest(RadioTestCase):
    """Compact header roster: live handles inline, the rest as +N."""

    def test_live_inline_dead_counted(self):
        """Live handles are listed inline and the rest counted."""
        self.add_handle("kimi", ref="herdr:1-1")
        self.add_handle("codex", ref="manual")  # no pane binding -> pull dot
        handles = self.conn.execute("SELECT * FROM handles ORDER BY name").fetchall()
        states = {"1-1": {"label": "kimi", "agent": None, "agent_status": "idle"}}
        roster = radio_view.compact_roster(handles, states)
        self.assertEqual(roster.plain, "●kimi +1")

    def test_all_dead_is_only_the_count(self):
        """With no live handles only the count shows."""
        self.add_handle("kimi", ref="manual")
        self.add_handle("codex", ref="herdr:9-9")  # pane gone -> ✗
        handles = self.conn.execute("SELECT * FROM handles ORDER BY name").fetchall()
        roster = radio_view.compact_roster(handles, {})
        self.assertEqual(roster.plain, " +2")


@unittest.skipUnless(HAS_VIEW, "view deps (textual) not installed")
@unittest.skipUnless(HAS_VIEW, "view deps (textual) not installed")
class ViewLedgerStateTest(RadioTestCase):
    """radio-view on a ledger that is not there yet: an empty state, no
    created file, and a relay probe that never crashes."""

    def setUp(self):
        """Point the view module at a path this test controls."""
        super().setUp()
        self._saved_view = (radio_view.DB_PATH, radio_view.LOCK_PATH)
        self.addCleanup(self._restore_view)
        radio_view.DB_PATH = radio.STATE_DIR / "view-state" / "radio.db"
        radio_view.LOCK_PATH = radio.STATE_DIR / "view-state" / "relay.lock"

    def _restore_view(self):
        """Put the view's paths back."""
        radio_view.DB_PATH, radio_view.LOCK_PATH = self._saved_view

    def test_missing_ledger_reads_as_none_and_is_not_created(self):
        """A dashboard opened before radio leaves no empty radio.db behind."""
        self.assertFalse(radio_view.DB_PATH.exists())
        self.assertIsNone(radio_view.read_rows("SELECT 1"))
        self.assertFalse(radio_view.DB_PATH.exists())

    def test_empty_and_foreign_files_read_as_none(self):
        """An empty or foreign file is the empty state, not a traceback."""
        radio_view.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        radio_view.DB_PATH.write_text("", encoding="utf-8")
        self.assertIsNone(radio_view.read_rows("SELECT 1"))
        radio_view.DB_PATH.write_bytes(b"not a database at all")
        self.assertIsNone(radio_view.read_rows("SELECT 1"))

    def test_initialized_ledger_reads_rows(self):
        """An initialized ledger reads normally through the same helper."""
        radio_view.DB_PATH = radio.DB_PATH
        self.add_handle("bob")
        rows = radio_view.read_rows("SELECT name FROM handles")
        self.assertEqual([row["name"] for row in rows], ["bob"])

    def test_relay_probe_is_total(self):
        """A missing, held or unreadable lock reports a state, never raises."""
        self.assertFalse(radio_view.relay_alive())
        radio_view.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        radio_view.LOCK_PATH.write_text("", encoding="utf-8")
        self.assertFalse(radio_view.relay_alive())  # nobody holds it
        if os.name != "nt":
            import fcntl

            holder = open(radio_view.LOCK_PATH, "a+")
            self.addCleanup(holder.close)
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(radio_view.relay_alive())  # a holder means up
            holder.close()
        radio_view.LOCK_PATH.chmod(0o000)
        self.assertFalse(radio_view.relay_alive())  # cannot even open it
        radio_view.LOCK_PATH.chmod(0o600)


@unittest.skipUnless(HAS_VIEW, "view deps (textual) not installed")
class ViewWorkerTest(unittest.IsolatedAsyncioTestCase):
    """The roster scan runs on a worker thread: a slow herdr — one call per
    workspace, each with its own timeout — must not freeze the dashboard. The
    synchronous query tests cannot catch a scan moving back onto the event
    loop."""

    async def asyncSetUp(self):
        """Point the view at a temp ledger with one handle and a slow herdr."""
        self.tmp = tempfile.TemporaryDirectory(prefix="radio-view-worker-")
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "HERDR_ENV": "", "HERDR_WORKSPACE_ID": "", "RADIO_VIEW_FREQUENCY": "",
        })
        env.start()
        self.addCleanup(env.stop)
        for name, value in (("DB_PATH", Path(self.tmp.name) / "radio.db"),
                            ("LOCK_PATH", Path(self.tmp.name) / "relay.lock")):
            self.addCleanup(setattr, radio_view, name, getattr(radio_view, name))
            setattr(radio_view, name, value)
        conn = sqlite3.connect(radio_view.DB_PATH)
        conn.executescript("""
            CREATE TABLE handles (workspace TEXT, name TEXT, session_ref TEXT,
                                  pane_workspace TEXT, agent TEXT);
            CREATE TABLE messages (id INTEGER PRIMARY KEY, from_ws TEXT, to_ws TEXT,
                                   from_handle TEXT, to_handle TEXT, ts TEXT, text TEXT);
            CREATE TABLE deliveries (target_ws TEXT, status TEXT);
        """)
        conn.execute("INSERT INTO handles VALUES ('w1','bob','herdr:w1:p1','','codex')")
        conn.commit()
        conn.close()
        self.addCleanup(setattr, radio_view, "herdr", radio_view.herdr)

        def slow_herdr(*args, **kwargs):
            """A herdr stub that sleeps 0.3s per call and reports bob's pane."""
            time.sleep(0.3)
            payload = {"result": {"workspaces": [{"workspace_id": "w1", "label": "Main"}]}}
            if args[:2] == ("pane", "list"):
                payload = {"result": {"panes": [{
                    "pane_id": "w1:p1", "label": "bob", "workspace_id": "w1",
                    "agent": "codex", "agent_status": "idle",
                }]}}
            return subprocess.CompletedProcess(list(args), 0, json.dumps(payload), "")

        radio_view.herdr = slow_herdr
        self.app = radio_view.RadioView()

    async def test_scan_returns_immediately_and_still_lands(self):
        """refresh_handles schedules instead of scanning, and the scan lands."""
        async with self.app.run_test() as pilot:
            await pilot.pause(0.1)
            start = time.perf_counter()
            self.app.refresh_handles()
            self.assertLess(time.perf_counter() - start, 0.2)
            await pilot.pause(1.5)
            sidebar = self.app.query_one("#handles").render().plain
            self.assertIn("bob", sidebar)
            self.assertIn("w1:p1 idle", sidebar)


@unittest.skipUnless(HAS_VIEW, "view deps (textual) not installed")
class ViewScopeTest(RadioTestCase):
    """The view starts scoped to its pane's workspace (`a` toggles all): the
    scope comes from the herdr env and the query builders filter handles and
    messages without leaking another workspace's traffic."""

    def test_scope_from_herdr_env(self):
        """The scope comes from HERDR_WORKSPACE_ID only inside herdr."""
        os.environ["HERDR_ENV"] = "1"
        os.environ["HERDR_WORKSPACE_ID"] = "w2"
        self.addCleanup(os.environ.pop, "HERDR_ENV", None)
        self.addCleanup(os.environ.pop, "HERDR_WORKSPACE_ID", None)
        self.assertEqual(radio_view.workspace_scope(), "w2")
        os.environ.pop("HERDR_ENV")
        self.assertEqual(radio_view.workspace_scope(), "")

    def test_handles_query_scopes(self):
        """Scoped and all-workspace handle queries differ as expected."""
        sql, params = radio_view.handles_query("w2", False)
        self.assertIn("WHERE workspace = ?", sql)
        self.assertEqual(params, ("w2",))
        sql, params = radio_view.handles_query("w2", True)
        self.assertNotIn("WHERE workspace", sql)
        self.assertEqual(params, ())

    def test_messages_query_scopes(self):
        """Scoped messages filter on both ends; outside herdr there is no filter."""
        sql, params = radio_view.messages_query("w2", False, 5)
        self.assertIn("from_ws = ? OR to_ws = ?", sql)
        self.assertEqual(params, (5, "w2", "w2"))
        sql, params = radio_view.messages_query("w2", True, 5)
        self.assertNotIn("from_ws", sql)
        self.assertEqual(params, (5,))
        # Outside Herdr there is no scope to honor.
        sql, params = radio_view.messages_query("", False, 5)
        self.assertEqual(params, (5,))


class WorkspaceDeliveryTest(RadioTestCase):
    """End-to-end (monkeypatched herdr): a handle bound to a non-default
    workspace gets push delivery. bin/radio needs no workspace scan — pane
    ids carry the workspace prefix (w2:p1) and herdr resolves them globally
    in pane get/send-text/send-keys (verified live against herdr)."""

    def test_relay_pushes_to_non_default_workspace(self):
        """A pane id prefixed with another workspace travels unchanged and gets pushed."""
        self.add_handle("bob", ref="herdr:w2:p1")
        mid = self.pm("alice", "bob", "hi")
        orig_fetch, orig_push = radio.fetch_pane, radio.push_to_pane
        self.addCleanup(setattr, radio, "fetch_pane", orig_fetch)
        self.addCleanup(setattr, radio, "push_to_pane", orig_push)
        fetched, pushed = [], []
        radio.fetch_pane = lambda pane_id: (fetched.append(pane_id), {"label": "bob"})[1]
        radio.push_to_pane = lambda pane_id, text: (pushed.append(pane_id), (True, None))[1]
        radio.relay_tick(self.conn)
        # The workspace-prefixed id travels unchanged from ledger to herdr.
        # Delivery rechecks the pane under the retune lock before pushing.
        self.assertTrue(fetched)
        self.assertEqual(set(fetched), {"w2:p1"})
        self.assertEqual(pushed, ["w2:p1"])
        self.assertEqual(self.delivery_row(mid)["status"], "delivered")


@unittest.skipUnless(HAS_VIEW, "view deps (textual) not installed")
class WorkspacePaneStatesTest(RadioTestCase):
    """Workspace-aware pane discovery (radio-view): merge across workspaces,
    fallback to the default scan when `workspace list` fails, skip a
    workspace that vanishes mid-scan."""

    WS_TWO = json.dumps(
        {"result": {"workspaces": [{"workspace_id": "w1"}, {"workspace_id": "w2"}]}}
    )

    @staticmethod
    def _panes(*pane_ids):
        """Build a pane-list JSON payload for the given ids."""
        return json.dumps(
            {"result": {"panes": [{"pane_id": p, "label": "x"} for p in pane_ids]}}
        )

    def setUp(self):
        """Capture the view module's herdr function and the calls."""
        super().setUp()
        self._herdr = radio_view.herdr
        self.addCleanup(self._restore)
        self.calls = []

    def _restore(self):
        """Put the view module's herdr back."""
        radio_view.herdr = self._herdr

    def install(self, routes):
        """Script radio_view.herdr: argv tuple -> (stdout, rc), or None (binary gone)."""
        def fake(*args):
            """herdr stand-in that answers from the scripted route table."""
            self.calls.append(args)
            route = routes.get(tuple(args), ("{}", 1))
            if route is None:
                return None
            stdout, rc = route
            return subprocess.CompletedProcess(list(args), rc, stdout=stdout, stderr="")

        radio_view.herdr = fake

    def test_merges_all_workspaces(self):
        """Pane states merge across every workspace, not just the default."""
        self.install({
            ("workspace", "list"): (self.WS_TWO, 0),
            ("pane", "list", "--workspace", "w1"): (self._panes("w1:p1"), 0),
            ("pane", "list", "--workspace", "w2"): (self._panes("w2:p3", "w2:p4"), 0),
        })
        states = radio_view.pane_states()
        self.assertEqual(set(states), {"w1:p1", "w2:p3", "w2:p4"})
        # The default-workspace-only scan must not be used on the merged path.
        self.assertNotIn(("pane", "list"), self.calls)

    def test_workspace_list_failure_falls_back_to_default_scan(self):
        """A failed, bad or missing workspace list falls back to the default pane scan."""
        routes = {
            ("workspace", "list"): ("", 1),  # herdr error
            ("pane", "list"): (self._panes("w1:p1"), 0),
        }
        self.install(routes)
        self.assertEqual(set(radio_view.pane_states()), {"w1:p1"})
        self.assertIn(("pane", "list"), self.calls)
        # Bad JSON and a missing herdr binary fall back the same way.
        self.calls.clear()
        self.install({**routes, ("workspace", "list"): ("not-json", 0)})
        self.assertEqual(set(radio_view.pane_states()), {"w1:p1"})
        self.calls.clear()
        self.install({**routes, ("workspace", "list"): None})
        self.assertEqual(set(radio_view.pane_states()), {"w1:p1"})

    def test_workspace_vanishing_mid_scan_is_skipped(self):
        """A workspace that errors mid-scan is skipped."""
        self.install({
            ("workspace", "list"): (self.WS_TWO, 0),
            ("pane", "list", "--workspace", "w1"): (self._panes("w1:p1"), 0),
            ("pane", "list", "--workspace", "w2"): ("", 1),  # closed mid-scan
        })
        states = radio_view.pane_states()
        self.assertEqual(set(states), {"w1:p1"})

    def test_dot_for_handle_in_non_default_workspace(self):
        """A handle in another workspace gets its live dot."""
        self.add_handle("bob", ref="herdr:w2:p3")
        row = self.conn.execute("SELECT * FROM handles WHERE name='bob'").fetchone()
        states = {"w2:p3": {"label": "bob", "agent_status": "idle"}}
        dot, note = radio_view.dot_for(row, states)
        self.assertEqual((dot.plain, note), ("●", "idle"))

    def test_duplicate_labels_across_workspaces_stay_keyed_by_pane_id(self):
        # Two workspaces may hold a pane with the same label: dot_for looks
        # the pane up by its workspace-prefixed id; the label is only the
        # identity check, never the lookup key.
        """The pane is looked up by its prefixed id, not by label."""
        self.add_handle("bob", ref="herdr:w2:p1", agent="claude")
        row = self.conn.execute("SELECT * FROM handles WHERE name='bob'").fetchone()
        states = {
            "w1:p1": {"label": "bob", "agent": "claude", "agent_status": "working"},
            "w2:p1": {"label": "bob", "agent": "claude", "agent_status": "idle"},
        }
        dot, note = radio_view.dot_for(row, states)
        self.assertEqual((dot.plain, note), ("●", "idle"))  # w2's pane, not w1's


class UsageToolsTest(RadioTestCase):
    """radio tools usage: provider quota read in-process from each provider's
    own files, bounded by one cache and one lock — never a poll."""

    def setUp(self):
        """Stub the provider reads so no test reaches a provider."""
        super().setUp()
        self._read = radio.read_provider_usage
        self.addCleanup(setattr, radio, "read_provider_usage", self._read)
        self.reads = []

    def entry(self, remaining=61.0):
        """A minimal provider entry shaped like the real readers return."""
        return {
            "plan": "test",
            "windows": [
                {"id": "primary", "label": "Week", "remainingPercent": remaining, "resetsAt": None}
            ],
        }

    def install(self, value=None, fail=False):
        """Record each provider read and answer with the configured value."""
        def read(target):
            self.reads.append(target["label"])
            return None if fail else (value or self.entry())
        radio.read_provider_usage = read

    def add_account(self, name, provider, home):
        """Insert one account row, as `radio account add` would."""
        self.conn.execute(
            "INSERT INTO accounts(name, provider, home, env, created_at) VALUES (?,?,?,?,?)",
            (name, provider, home, "{}", radio.now()),
        )
        self.conn.commit()

    def backdate_cache(self, seconds=radio.USAGE_CACHE_TTL_S + 60):
        """Age every cache entry past the TTL so the next refresh must read again."""
        path = radio.usage_cache_path()
        payload = json.loads(path.read_text(encoding="utf-8"))
        for entry in payload["entries"].values():
            entry["observedAt"] = time.time() - seconds
        path.write_text(json.dumps(payload), encoding="utf-8")

    def run_usage(self, **flags):
        """Run cmd_tools_usage with stdout captured and return the printed text."""
        buf = io.StringIO()
        args = radio.build_parser().parse_args(["tools", "usage"])
        for key, value in flags.items():
            setattr(args, key, value)
        with contextlib.redirect_stdout(buf):
            radio.cmd_tools_usage(self.conn, args)
        return buf.getvalue()

    def test_targets_cover_defaults_and_named_accounts(self):
        """Every codex/kimi account row is read; Claude's shared login stays one entry."""
        self.add_account("work", "codex", "/tmp/codex-work")
        self.add_account("personal", "kimi", "/tmp/kimi-personal")
        self.add_account("alt", "claude", "/tmp/claude-alt")
        labels = [target["label"] for target in radio.usage_targets(self.conn)]
        self.assertIn("Codex", labels)
        self.assertIn("Claude", labels)
        self.assertIn("Kimi", labels)
        self.assertIn("Codex · work", labels)
        self.assertIn("Kimi · personal", labels)
        self.assertNotIn("Claude · alt", labels)

    def test_refresh_reads_once_per_window(self):
        """A second refresh inside the TTL reads nothing; an aged cache reads again."""
        self.install()
        radio.refresh_usage_cache(self.conn)
        first = len(self.reads)
        self.assertEqual(first, len(radio.usage_targets(self.conn)))
        radio.refresh_usage_cache(self.conn)
        self.assertEqual(len(self.reads), first)
        self.backdate_cache()
        radio.refresh_usage_cache(self.conn)
        self.assertEqual(len(self.reads), first * 2)

    def test_failed_read_keeps_the_previous_value_and_is_not_retried(self):
        """A provider failure keeps the cached value, stamps the attempt, and
        is asked again only in the next window — never on every call."""
        self.install()
        radio.refresh_usage_cache(self.conn)
        self.backdate_cache()
        kept = radio.load_usage_cache()["Codex"]
        self.install(fail=True)
        before = len(self.reads)
        entries = radio.refresh_usage_cache(self.conn)
        self.assertEqual(len(self.reads), before + len(radio.usage_targets(self.conn)))
        self.assertEqual(
            {key: value for key, value in entries["Codex"].items() if key != "attemptedAt"},
            kept,
        )
        self.assertGreater(entries["Codex"]["attemptedAt"], 0)
        # Inside the window the failure is cached: a second call reads nothing.
        radio.refresh_usage_cache(self.conn)
        self.assertEqual(len(self.reads), before + len(radio.usage_targets(self.conn)))
        path = radio.usage_cache_path()
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["entries"].pop("Kimi")
        payload["entries"]["Claude"]["observedAt"] = time.time() - radio.USAGE_CACHE_TTL_S - 60
        path.write_text(json.dumps(payload), encoding="utf-8")
        entries = radio.refresh_usage_cache(self.conn)
        self.assertEqual(entries["Kimi"]["windows"], [])
        self.assertIn("unavailable", entries["Kimi"]["detail"])

    def test_line_table_and_json_render_the_cache(self):
        """The three renderers read the same cache entries."""
        entries = {
            "Codex": {"provider": "codex", "observedAt": time.time(), "windows": [
                {"id": "primary", "label": "5h", "remainingPercent": 98.0, "resetsAt": None},
                {"id": "secondary", "label": "Week", "remainingPercent": 61.0, "resetsAt": None},
            ]},
            "Claude": {"provider": "claude", "observedAt": time.time(), "windows": [
                {"id": "seven_day", "label": "Week", "remainingPercent": 85.0, "resetsAt": None},
            ]},
            "Kimi": {"provider": "kimi", "observedAt": time.time(),
                     "detail": "credentials or endpoint unavailable", "windows": []},
        }
        radio.save_usage_cache(entries)
        self.install()
        line = self.run_usage(once=True).strip()
        self.assertIn("Codex: Week 61%", line)
        self.assertIn("Kimi: unavailable", line)
        table = self.run_usage(table=True)
        self.assertIn("Codex", table)
        self.assertIn("Week", table)
        payload = json.loads(self.run_usage(json=True))
        self.assertEqual(
            [account["label"] for account in payload["accounts"]], ["Codex", "Claude", "Kimi"]
        )
        self.assertEqual(payload["accounts"][0]["windows"][1]["remainingPercent"], 61.0)

    def test_malformed_cache_entries_do_not_break_the_display(self):
        """A truncated or hand-edited cache is an empty entry, not a crash."""
        path = radio.usage_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "entries": {
            "Codex": "not an object",
            "Kimi": {"observedAt": "yesterday", "windows": [{"label": "Week"}]},
        }}), encoding="utf-8")
        self.assertEqual(list(radio.load_usage_cache()), ["Kimi"])
        # A malformed timestamp and a non-list windows value render as empty.
        entries = {"Kimi": {"observedAt": "yesterday", "windows": "junk"}}
        self.assertIn("Kimi: unavailable", radio.render_usage_line(entries))
        self.assertIn("no data yet", radio.render_usage_table(entries))
        payload = radio.usage_payload(entries)
        self.assertIsNone(payload["observedAt"])
        self.assertEqual(payload["accounts"][0]["windows"], [])

    def test_ticker_exits_cleanly_on_interrupt(self):
        """Ctrl-C ends the ticker without a traceback."""
        self.install()

        def stop(seconds):
            """End the ticker loop the way Ctrl-C does."""
            raise KeyboardInterrupt

        saved_sleep = radio.time.sleep
        self.addCleanup(setattr, radio.time, "sleep", saved_sleep)
        radio.time.sleep = stop
        args = radio.build_parser().parse_args(["tools", "usage"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(radio.cmd_tools_usage(self.conn, args), 0)

    def test_kimi_home_falls_back_like_the_cli(self):
        """KIMI_CODE_HOME wins, KIMI_HOME is honored, ~/.kimi-code is the default."""
        saved = {key: os.environ.pop(key, None) for key in ("KIMI_CODE_HOME", "KIMI_HOME")}
        self.addCleanup(self._restore_kimi_env, saved)

        def kimi_home():
            """The Kimi target's resolved home."""
            return [t for t in radio.usage_targets(self.conn) if t["label"] == "Kimi"][0]["home"]

        self.assertEqual(kimi_home(), Path.home() / ".kimi-code")
        os.environ["KIMI_HOME"] = "/tmp/kimi-home"
        self.assertEqual(kimi_home(), Path("/tmp/kimi-home"))
        os.environ["KIMI_CODE_HOME"] = "/tmp/kimi-code-home"
        self.assertEqual(kimi_home(), Path("/tmp/kimi-code-home"))

    @staticmethod
    def _restore_kimi_env(saved):
        """Put the ambient Kimi home env back."""
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_json_reports_cache_age_and_staleness(self):
        """The snapshot carries the observation time and a stale flag."""
        payload = radio.usage_payload({
            "Codex": {"provider": "codex", "observedAt": time.time() - 1200,
                      "windows": [{"id": "primary", "label": "Week", "remainingPercent": 61.0, "resetsAt": None}]}
        })
        self.assertTrue(payload["stale"])
        self.assertGreater(payload["ageSeconds"], radio.USAGE_CACHE_TTL_S)

    def test_parser_wires_the_tools_usage_command(self):
        """The CLI surface: tools usage with its one-shot flags."""
        args = radio.build_parser().parse_args(["tools", "usage", "--table"])
        self.assertTrue(args.table)
        self.assertEqual(args.func, radio.cmd_tools_usage)


class KimiUsageTest(RadioTestCase):
    """The Kimi reader: only the known regions are trusted, and a refreshed
    token file stays private."""

    def setUp(self):
        """Strip ambient Kimi region env so the fixture is hermetic."""
        super().setUp()
        self._kimi_env = {key: os.environ.pop(key, None) for key in
                          ("KIMI_CODE_BASE_URL", "KIMI_CODE_OAUTH_HOST", "KIMI_OAUTH_HOST")}
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        """Put the ambient Kimi env back."""
        for key, value in self._kimi_env.items():
            if value is not None:
                os.environ[key] = value

    def temp_home(self):
        """A temp Kimi home that cleans itself up."""
        tmp = tempfile.TemporaryDirectory(prefix="radio-kimi-")
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def test_unknown_region_is_refused(self):
        """A config pointing the refresh at another host is refused, not trusted."""
        home = self.temp_home()
        (home / "config.toml").write_text('oauthHost = "https://evil.example"\n', encoding="utf-8")
        self.assertIsNone(radio.kimi_runtime(home))

    def test_known_regions_resolve(self):
        """Both shipped regions resolve; only the mainland uses the default key."""
        mainland = radio.kimi_runtime(self.temp_home())
        self.assertEqual(mainland[:2], (radio.KIMI_MAINLAND_OAUTH_HOST, radio.KIMI_MAINLAND_BASE_URL))
        self.assertEqual(mainland[2].name, "kimi-code.json")
        global_home = self.temp_home()
        (global_home / "config.toml").write_text(
            f'oauthHost = "{radio.KIMI_GLOBAL_OAUTH_HOST}"\n', encoding="utf-8"
        )
        overseas = radio.kimi_runtime(global_home)
        self.assertEqual(overseas[:2], (radio.KIMI_GLOBAL_OAUTH_HOST, radio.KIMI_GLOBAL_BASE_URL))
        self.assertNotEqual(overseas[2].name, "kimi-code.json")

    def test_refreshed_credentials_are_written_private(self):
        """A refresh keeps the token file unreadable to group and others."""
        path = self.temp_home() / "credentials" / "kimi-code.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"refresh_token": "old"}), encoding="utf-8")
        os.chmod(path, 0o644)
        saved = radio.usage_http_post_form
        self.addCleanup(setattr, radio, "usage_http_post_form", saved)
        radio.usage_http_post_form = lambda url, form: (
            200, {"access_token": "new", "refresh_token": "next", "expires_in": 3600}
        )
        token = radio.kimi_refresh(radio.KIMI_MAINLAND_OAUTH_HOST, path, {"refresh_token": "old"})
        self.assertEqual(token, "new")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["refresh_token"], "next")
        if os.name != "nt":  # POSIX mode bits; Windows has no group/other bit
            self.assertEqual(path.stat().st_mode & 0o077, 0)


class ToolsCalmTest(RadioTestCase):
    """radio tools calm: a self-contained terminal animation, no ledger or deps."""

    def test_parser_wires_the_calm_command(self):
        """The CLI surface: tools calm parses to its own handler."""
        args = radio.build_parser().parse_args(["tools", "calm"])
        self.assertEqual(args.func, radio.cmd_tools_calm)

    def test_no_terminal_is_a_clear_message(self):
        """Without a tty the command explains itself instead of a curses traceback."""
        saved = sys.stdout
        sys.stdout = io.StringIO()
        self.addCleanup(setattr, sys, "stdout", saved)
        with self.assertRaises(SystemExit) as ctx:
            radio.cmd_tools_calm(argparse.Namespace())
        self.assertIn("terminal", str(ctx.exception))

    def test_scene_is_seeded_from_the_screen(self):
        """Stars and motes scale with the screen and stay inside its bounds."""
        stars = radio.calm_stars(80, 24)
        self.assertGreaterEqual(len(stars), 12)
        motes = radio.calm_motes(80, 24)
        self.assertTrue(motes)
        self.assertTrue(all(0.0 <= mote.x <= 1.0 and 0.0 <= mote.y <= 1.0 for mote in motes))
        self.assertTrue(all(mote.glyph in radio.CALM_MOTE_GLYPHS for mote in motes))


if __name__ == "__main__":
    unittest.main()



