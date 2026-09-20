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
        self.conn.close()
        radio.STATE_DIR, radio.DB_PATH, radio.LOCK_PATH = self._saved
        for key, value in self._saved_env.items():
            if value is not None:
                os.environ[key] = value

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


class RelayIdentityGuardTest(RadioTestCase):
    """The relay must never push one handle's mail into a pane that no longer
    belongs to it — a live pane id proves nothing (panes get reused)."""

    def setUp(self):
        super().setUp()
        self._fetch = radio.fetch_pane
        self._push = radio.push_to_pane
        self.addCleanup(self._restore)
        self.push_calls = []

    def _restore(self):
        radio.fetch_pane = self._fetch
        radio.push_to_pane = self._push

    def tick(self, pane):
        radio.fetch_pane = lambda pane_id: pane

        def push(pane_id, text):
            self.push_calls.append(pane_id)
            return True, None

        radio.push_to_pane = push
        return radio.relay_tick(self.conn)

    def test_label_mismatch_leaves_for_pull_without_push(self):
        self.add_handle("bob", ref="herdr:w1:p1")
        mid = self.pm("alice", "bob", "hi")
        self.tick({"label": "mallory"})
        row = self.delivery_row(mid)
        self.assertEqual(row["status"], "pull")
        self.assertEqual(row["last_error"], "pane reused")
        self.assertEqual(self.push_calls, [])

    def test_agent_mismatch_leaves_for_pull_without_push(self):
        self.add_handle("bob", ref="herdr:w1:p1", agent="claude", agent_session="sess1")
        mid = self.pm("alice", "bob", "hi")
        self.tick({"label": "bob", "agent": "codex"})
        row = self.delivery_row(mid)
        self.assertEqual(row["status"], "pull")
        self.assertEqual(row["last_error"], "agent changed")
        self.assertEqual(self.push_calls, [])

    def test_matching_pane_gets_the_push(self):
        self.add_handle("bob", ref="herdr:w1:p1", agent="claude", agent_session="sess1")
        mid = self.pm("alice", "bob", "hi")
        self.tick({"label": "bob", "agent": "claude"})
        self.assertEqual(self.push_calls, ["w1:p1"])
        self.assertEqual(self.delivery_row(mid)["status"], "delivered")


class DeliveryUnconfirmedTest(RadioTestCase):
    def setUp(self):
        super().setUp()
        self.add_handle("bob", ref="herdr:w1:p1")
        self.mid = self.pm("alice", "bob", "hi")
        self._fetch = radio.fetch_pane
        self._push = radio.push_to_pane
        self.addCleanup(self._restore)
        self.push_calls = []

    def _restore(self):
        radio.fetch_pane = self._fetch
        radio.push_to_pane = self._push

    def test_unconfirmed_goes_to_pull_and_is_never_retried(self):
        radio.fetch_pane = lambda pane_id: {"label": "bob"}

        def push(pane_id, text):
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


class BacktickNormalizationTest(RadioTestCase):
    def test_backticks_become_quotes(self):
        # A backtick typed into a shell pane is command substitution.
        mid = self.pm("alice", "bob", "run `ls -la` now")
        self.assertEqual(self.message_row(mid)["text"], "run 'ls -la' now")


class NormalizeHandleTest(RadioTestCase):
    def test_strip_at_and_validate(self):
        self.assertEqual(radio.normalize_handle("@foo"), "foo")
        self.assertEqual(radio.normalize_handle("foo"), "foo")
        with self.assertRaises(SystemExit):
            radio.normalize_handle("bad handle")
        with self.assertRaises(SystemExit):
            radio.normalize_handle("we]rd")

    def test_pm_to_at_handle_resolves(self):
        self.add_handle("bob")
        args = argparse.Namespace(sender="alice", to="@bob", text=["hi"],
                                  ref=None, reply_required=False)
        with contextlib.redirect_stdout(io.StringIO()):
            radio.cmd_pm(self.conn, args)
        row = self.conn.execute("SELECT * FROM messages").fetchone()
        self.assertEqual(row["to_handle"], "bob")


def join_args(handle, pane=None):
    return argparse.Namespace(handle=handle, pane=pane, provider=None,
                              new=False, resume=False, model=None, no_launch=True)


class ResolveHandleTest(RadioTestCase):
    def test_resolver_semantics(self):
        self.add_handle("bob")
        self.assertEqual(radio.resolve_handle(self.conn, "bob"), "bob")
        self.assertEqual(radio.resolve_handle(self.conn, "BOB"), "bob")
        self.assertIsNone(radio.resolve_handle(self.conn, "ghost"))

    def test_ambiguous_case_variants_exit(self):
        self.add_handle("Foo")
        self.add_handle("foo")
        with self.assertRaises(SystemExit):
            radio.resolve_handle(self.conn, "FOO")

    def test_pm_case_insensitive_uses_registered_spelling(self):
        self.add_handle("bob")
        args = argparse.Namespace(sender="alice", to="BOB", text=["hi"],
                                  ref=None, reply_required=False)
        with contextlib.redirect_stdout(io.StringIO()):
            radio.cmd_pm(self.conn, args)
        row = self.conn.execute("SELECT * FROM messages").fetchone()
        self.assertEqual(row["to_handle"], "bob")

    def test_part_wrong_case_removes_the_right_row(self):
        self.add_handle("bob")
        with contextlib.redirect_stdout(io.StringIO()):
            radio.cmd_part(self.conn, argparse.Namespace(handle="BOB"))
        row = self.conn.execute("SELECT name FROM handles").fetchone()
        self.assertIsNone(row)

    def test_join_case_variant_upserts_existing(self):
        self.add_handle("bob", ref="manual")
        with contextlib.redirect_stdout(io.StringIO()):
            radio.cmd_join(self.conn, join_args("BOB"))
        rows = self.conn.execute(
            "SELECT name FROM handles WHERE LOWER(name) = 'bob'"
        ).fetchall()
        self.assertEqual([r["name"] for r in rows], ["bob"])


class CatchUpCompactionTest(RadioTestCase):
    def setUp(self):
        super().setUp()
        self._fetch = radio.fetch_pane
        self._pane_exists = radio.pane_exists
        self.addCleanup(self._restore)

    def _restore(self):
        radio.fetch_pane = self._fetch
        radio.pane_exists = self._pane_exists

    def seed_backlog(self):
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
        return {
            r["message_id"]: (r["status"], r["last_error"])
            for r in self.conn.execute("SELECT * FROM deliveries").fetchall()
        }

    def test_pane_rejoin_compacts_backlog(self):
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
        m1, m2, m3, m4, m5, m6 = self.seed_backlog()
        radio.pane_exists = lambda pane_id: False  # old pane gone: ref stays manual
        with contextlib.redirect_stdout(io.StringIO()):
            radio.cmd_join(self.conn, join_args("bob"))
        got = self.statuses()
        for mid in (m1, m2, m3, m4, m5):
            self.assertEqual(got[mid][0], "pending")
        self.assertEqual(got[m6][0], "failed")


class RepromotionTest(RadioTestCase):
    def setUp(self):
        super().setUp()
        self.add_handle("bob", ref="herdr:w1:p1")
        self._fetch = radio.fetch_pane
        self._push = radio.push_to_pane
        self.addCleanup(self._restore)
        self.push_calls = []

    def _restore(self):
        radio.fetch_pane = self._fetch
        radio.push_to_pane = self._push

    def stranded(self, last_error):
        mid = self.pm("alice", "bob", "hi")
        self.conn.execute("UPDATE deliveries SET status='pull', last_error=? "
                          "WHERE message_id=?", (last_error, mid))
        self.conn.commit()
        return mid

    def tick(self):
        radio.fetch_pane = lambda pane_id: {"label": "bob"}

        def push(pane_id, text):
            self.push_calls.append(pane_id)
            return True, None

        radio.push_to_pane = push
        return radio.relay_tick(self.conn)

    def test_no_live_pane_is_repromoted_and_pushed(self):
        mid = self.stranded("no live pane")
        self.tick()
        self.assertEqual(self.push_calls, ["w1:p1"])
        self.assertEqual(self.delivery_row(mid)["status"], "delivered")

    def test_deliberate_pull_stays_pull(self):
        m1 = self.stranded("catch-up: read via radio inbox")
        m2 = self.stranded("delivery unconfirmed — left for pull, not retried")
        self.tick()
        self.assertEqual(self.push_calls, [])
        self.assertEqual(self.delivery_row(m1)["status"], "pull")
        self.assertEqual(self.delivery_row(m2)["status"], "pull")


if __name__ == "__main__":
    unittest.main()
