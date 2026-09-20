#!/usr/bin/env python3
"""Tests for bin/radio: ledger semantics, envelope format, launch argv matrix,
and relay delivery outcomes. herdr and pane I/O are monkeypatched — no test
spawns a process, execs, or touches the real ~/.local/share/herdr-radio."""

import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import sqlite3
import tempfile
import unittest
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
    def setUp(self):
        self._saved = (radio.STATE_DIR, radio.DB_PATH, radio.LOCK_PATH)
        tmp = Path(tempfile.mkdtemp(prefix="radio-test-"))
        radio.STATE_DIR = tmp
        radio.DB_PATH = tmp / "radio.db"
        radio.LOCK_PATH = tmp / "relay.lock"
        self.conn = radio.connect()

    def tearDown(self):
        self.conn.close()
        radio.STATE_DIR, radio.DB_PATH, radio.LOCK_PATH = self._saved

    def add_handle(self, name, ref="manual", agent=None, agent_session=None,
                   last_seen=None):
        ts = last_seen or radio.now()
        self.conn.execute(
            "INSERT INTO handles(name, session_ref, agent, agent_session, created_at, last_seen) "
            "VALUES (?,?,?,?,?,?)",
            (name, ref, agent, agent_session, ts, ts),
        )
        self.conn.commit()

    def pm(self, sender, to, text, ref=None, reply_required=False):
        mid = radio.record_message(self.conn, "pm", sender, text,
                                   to_handle=to, ref=ref, reply_required=reply_required)
        radio.enqueue_delivery(self.conn, mid, to)
        self.conn.commit()
        return mid

    def message_row(self, mid):
        return self.conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()

    def delivery_row(self, mid):
        return self.conn.execute(
            "SELECT * FROM deliveries WHERE message_id = ?", (mid,)
        ).fetchone()


class PaneIdOfTest(RadioTestCase):
    def test_herdr_ref_vs_manual(self):
        self.assertEqual(radio.pane_id_of("herdr:w1:p1"), "w1:p1")
        self.assertIsNone(radio.pane_id_of("manual"))


class FormatForPaneTest(RadioTestCase):
    def test_envelope_variants(self):
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
    def test_handle_and_no_polling_rule(self):
        text = radio.briefing_text("scout")
        self.assertIn('You are on Radio as "scout"', text)
        self.assertIn("NEVER poll `radio inbox`", text)


class LaunchArgvTest(RadioTestCase):
    def test_provider_matrix(self):
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
    def test_push_vs_pull_binding(self):
        self.add_handle("onpane", ref="herdr:w1:p1")
        mid = self.pm("alice", "onpane", "hi")
        self.assertEqual(self.delivery_row(mid)["status"], "pending")

        self.add_handle("offgrid", ref="manual")
        mid = self.pm("alice", "offgrid", "hi")
        self.assertEqual(self.delivery_row(mid)["status"], "pull")


class CmdPmTest(RadioTestCase):
    def pm_args(self, to, text, reply_required=False):
        return argparse.Namespace(sender="alice", to=to, text=[text],
                                  ref=None, reply_required=reply_required)

    def run_pm(self, args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            radio.cmd_pm(self.conn, args)
        return buf.getvalue()

    def test_unknown_target_exits(self):
        with self.assertRaises(SystemExit):
            radio.cmd_pm(self.conn, self.pm_args("ghost", "hi"))

    def test_happy_path_records_and_prints(self):
        self.add_handle("bob", ref="herdr:w1:p1")
        out = self.run_pm(self.pm_args("bob", "hi"))
        self.assertIn("pm #1 -> bob", out)
        row = self.delivery_row(1)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["target"], "bob")

    def test_long_message_nudges_ref(self):
        self.add_handle("bob")
        out = self.run_pm(self.pm_args("bob", "x" * 1300))
        self.assertIn("--ref <path>", out)

    def test_reply_required_prints_no_poll_note(self):
        self.add_handle("bob")
        out = self.run_pm(self.pm_args("bob", "ping", reply_required=True))
        self.assertIn("do NOT poll `radio inbox`", out)


class PullPathTest(RadioTestCase):
    def test_show_marks_only_recipient_delivered(self):
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
    def test_part_fails_outstanding_deliveries(self):
        self.add_handle("bob", ref="manual")
        self.add_handle("onpane", ref="herdr:w1:p1")
        mid_pull = self.pm("alice", "bob", "a")
        mid_pending = self.pm("alice", "onpane", "b")
        with contextlib.redirect_stdout(io.StringIO()):
            radio.cmd_part(self.conn, argparse.Namespace(handle="bob"))
            radio.cmd_part(self.conn, argparse.Namespace(handle="onpane"))
        self.assertEqual(self.delivery_row(mid_pull)["status"], "failed")
        self.assertEqual(self.delivery_row(mid_pending)["status"], "failed")


class DeliveryWaitReasonTest(RadioTestCase):
    def handle_row(self, name):
        return self.conn.execute(
            "SELECT * FROM handles WHERE name = ?", (name,)
        ).fetchone()

    def test_reason_matrix(self):
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


class RelayTickTest(RadioTestCase):
    def setUp(self):
        super().setUp()
        self.add_handle("bob", ref="herdr:w1:p1")
        self.mid = self.pm("alice", "bob", "hi")
        self._fetch = radio.fetch_pane
        self._push = radio.push_to_pane
        self.addCleanup(self._restore)

    def _restore(self):
        radio.fetch_pane = self._fetch
        radio.push_to_pane = self._push

    def tick(self, pane, push_result):
        radio.fetch_pane = lambda pane_id: pane
        radio.push_to_pane = lambda pane_id, text: push_result
        return radio.relay_tick(self.conn)

    def test_push_ok_marks_delivered(self):
        events = self.tick({"label": "bob"}, (True, None))
        row = self.delivery_row(self.mid)
        self.assertEqual(row["status"], "delivered")
        self.assertIsNotNone(row["delivered_at"])
        self.assertTrue(any("delivered" in e for e in events))

    def test_no_pane_falls_back_to_pull(self):
        self.tick(None, (True, None))
        row = self.delivery_row(self.mid)
        self.assertEqual(row["status"], "pull")
        self.assertEqual(row["last_error"], "no live pane")

    def test_generic_failure_counts_attempts_then_fails(self):
        pane = {"label": "bob"}
        self.tick(pane, (False, "agent_not_accepting"))
        row = self.delivery_row(self.mid)
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["status"], "pending")

        self.conn.execute("UPDATE deliveries SET attempts=? WHERE message_id=?",
                          (radio.MAX_DELIVERY_ATTEMPTS - 1, self.mid))
        self.conn.commit()
        self.tick(pane, (False, "agent_not_accepting"))
        row = self.delivery_row(self.mid)
        self.assertEqual(row["attempts"], radio.MAX_DELIVERY_ATTEMPTS)
        self.assertEqual(row["status"], "failed")

    def test_agent_blocked_defers_without_attempt(self):
        events = self.tick({"label": "bob"}, (False, "agent_blocked"))
        row = self.delivery_row(self.mid)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)
        self.assertTrue(any("agent blocked" in e for e in events))


if __name__ == "__main__":
    unittest.main()
