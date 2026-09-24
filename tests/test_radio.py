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
import types
import unittest
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
                   last_seen=None, workspace="", role=None):
        ts = last_seen or radio.now()
        self.conn.execute(
            "INSERT INTO handles(workspace, name, session_ref, agent, agent_session, role, created_at, last_seen) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (workspace, name, ref, agent, agent_session, role, ts, ts),
        )
        self.conn.commit()

    def pm(self, sender, to, text, ref=None, reply_required=False,
           from_ws="", to_ws=""):
        mid = radio.record_message(self.conn, "pm", sender, text,
                                   to_handle=to, ref=ref, reply_required=reply_required,
                                   from_ws=from_ws, to_ws=to_ws)
        radio.enqueue_delivery(self.conn, mid, to, to_ws)
        self.conn.commit()
        return mid

    def message_row(self, mid):
        return self.conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()

    def delivery_row(self, mid):
        return self.conn.execute(
            "SELECT * FROM deliveries WHERE message_id = ?", (mid,)
        ).fetchone()

    def backdate_attempt(self, mid, seconds):
        old = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(
            timespec="milliseconds"
        )
        self.conn.execute("UPDATE deliveries SET last_attempt_at=? WHERE message_id=?",
                          (old, mid))
        self.conn.commit()


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
        # The first attempt just stamped last_attempt_at; the backoff window
        # would skip the retry, so back-date it to make the delivery due.
        self.backdate_attempt(self.mid, radio.RETRY_AFTER_S + 10)
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


def join_args(handle, pane=None, role=None):
    return argparse.Namespace(handle=handle, pane=pane, provider=None,
                              new=False, resume=False, model=None, role=role,
                              no_launch=True)


class ResolveHandleTest(RadioTestCase):
    def test_resolver_semantics(self):
        self.add_handle("bob")
        self.assertEqual(radio.resolve_handle(self.conn, "bob")["name"], "bob")
        self.assertEqual(radio.resolve_handle(self.conn, "BOB")["name"], "bob")
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


class RepairTest(RadioTestCase):
    """radio repair: a health report and --reset with a backup — the supported
    way out of a corrupt ledger or one written by a newer radio."""

    def run_repair(self, reset=False, yes=False):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = radio.cmd_repair(argparse.Namespace(reset=reset, yes=yes))
        return rc, buf.getvalue()

    def test_health_report_on_a_fresh_ledger(self):
        rc, out = self.run_repair()
        self.assertEqual(rc, 0)
        self.assertIn("verdict:   healthy", out)
        self.assertIn(f"schema:    {radio.SCHEMA_VERSION}", out)

    def test_newer_schema_is_reported_and_connect_refuses(self):
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
        saved = sys.stdin
        sys.stdin = io.StringIO("")
        self.addCleanup(setattr, sys, "stdin", saved)
        with self.assertRaises(SystemExit):
            self.run_repair(reset=True)

    def test_reset_recovers_from_a_newer_schema(self):
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
        saved = radio.herdr_json
        self.addCleanup(setattr, radio, "herdr_json", saved)
        radio.herdr_json = lambda *args: (data if data is not None else self.WS_LABELS)

    def test_same_name_in_two_workspaces(self):
        self.add_handle("reviewer", workspace="w1")
        self.add_handle("reviewer", workspace="w2")
        row = radio.resolve_handle(self.conn, "reviewer", "w2")
        self.assertEqual((row["workspace"], row["name"]), ("w2", "reviewer"))

    def test_other_workspace_handle_is_unreachable(self):
        self.add_handle("hede", workspace="w1")
        self.assertIsNone(radio.resolve_handle(self.conn, "hede", "w2"))

    def test_unscoped_handles_stay_reachable(self):
        self.add_handle("bot")
        self.add_handle("reviewer", workspace="w2")
        self.assertEqual(radio.resolve_handle(self.conn, "bot", "w2")["workspace"], "")

    def test_internal_qualified_form(self):
        self.add_handle("reviewer", workspace="w1")
        row = radio.resolve_handle(self.conn, "w1:reviewer", "")
        self.assertEqual(row["workspace"], "w1")

    def test_outside_scope_ambiguity_names_candidates(self):
        self.add_handle("reviewer", workspace="w1")
        self.add_handle("reviewer", workspace="w2")
        with self.assertRaises(SystemExit) as ctx:
            radio.resolve_handle(self.conn, "reviewer", "")
        self.assertIn("w1:reviewer", str(ctx.exception))
        self.assertIn("w2:reviewer", str(ctx.exception))

    def test_miss_message_names_the_workspace(self):
        self.stub_workspaces()
        self.add_handle("hede", workspace="w1")
        message = radio.no_handle_message(self.conn, "hede", "w2")
        self.assertIn('no handle "hede"', message)
        self.assertIn("Servers", message)
        self.assertIn("BoilerRoom", message)

    def test_workspace_spec_accepts_id_and_label(self):
        self.stub_workspaces()
        self.assertEqual(radio.resolve_workspace_spec("Servers", self.conn), "w2")
        self.assertEqual(radio.resolve_workspace_spec("w1", self.conn), "w1")
        with self.assertRaises(SystemExit):
            radio.resolve_workspace_spec("Nope", self.conn)


class ScopedPmTest(RadioTestCase):
    """cmd_pm routes inside the sender's workspace and refuses to cross."""

    def setUp(self):
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
        return argparse.Namespace(sender=None, to=to, text=["hi"], ref=None,
                                  reply_required=False)

    def test_pm_resolves_in_own_workspace(self):
        self.add_handle("alice", workspace="w2")
        self.add_handle("reviewer", workspace="w1")
        self.add_handle("reviewer", workspace="w2")
        with contextlib.redirect_stdout(io.StringIO()):
            radio.cmd_pm(self.conn, self.pm_args("reviewer"))
        row = self.conn.execute("SELECT * FROM messages").fetchone()
        self.assertEqual((row["from_ws"], row["from_handle"]), ("w2", "alice"))
        self.assertEqual((row["to_ws"], row["to_handle"]), ("w2", "reviewer"))

    def test_pm_refuses_other_workspace(self):
        self.add_handle("alice", workspace="w2")
        self.add_handle("hede", workspace="w1")
        with self.assertRaises(SystemExit) as ctx:
            radio.cmd_pm(self.conn, self.pm_args("hede"))
        message = str(ctx.exception)
        self.assertIn('no handle "hede"', message)
        self.assertIn("w2", message)

    def test_pm_warns_when_the_sender_is_not_joined(self):
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
        for name in ("fetch_pane", "push_to_pane"):
            saved = getattr(radio, name)
            self.addCleanup(setattr, radio, name, saved)
        radio.fetch_pane = lambda pane_id: pane
        self.pushed = []
        radio.push_to_pane = lambda pane_id, text: (self.pushed.append(pane_id), (True, None))[1]

    def test_delivery_records_target_workspace(self):
        self.add_handle("bob", ref="herdr:w2:p1", workspace="w2")
        mid = self.pm("alice", "bob", "hi", to_ws="w2")
        self.assertEqual(self.delivery_row(mid)["target_ws"], "w2")
        self.assertEqual(self.delivery_row(mid)["status"], "pending")

    def test_relay_pushes_inside_the_workspace(self):
        self.add_handle("bob", ref="herdr:w2:p1", workspace="w2")
        mid = self.pm("alice", "bob", "hi", to_ws="w2")
        self._stub_panes({"label": "bob", "workspace_id": "w2"})
        radio.relay_tick(self.conn)
        self.assertEqual(self.pushed, ["w2:p1"])
        self.assertEqual(self.delivery_row(mid)["status"], "delivered")

    def test_relay_refuses_foreign_workspace(self):
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
        super().setUp()
        saved = radio.current_workspace
        self.addCleanup(setattr, radio, "current_workspace", saved)
        radio.current_workspace = lambda: "w2"
        saved_json = radio.herdr_json
        self.addCleanup(setattr, radio, "herdr_json", saved_json)
        radio.herdr_json = lambda *args: {}

    def run_role(self, handle, text=(), clear=False):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            radio.cmd_role(
                self.conn,
                argparse.Namespace(handle=handle, text=list(text), clear=clear),
            )
        return buf.getvalue()

    def test_set_show_clear(self):
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
        self.add_handle("reviewer", workspace="w1")
        with self.assertRaises(SystemExit):
            self.run_role("reviewer", ["nope"])

    def test_briefing_carries_workspace_and_role(self):
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
        with contextlib.redirect_stdout(io.StringIO()):
            radio.cmd_join(self.conn, join_args("reviewer", pane="w2:p1",
                                                role="test evidence gate"))
        row = self.conn.execute("SELECT * FROM handles").fetchone()
        self.assertEqual((row["workspace"], row["name"]), ("w2", "reviewer"))
        self.assertEqual(row["role"], "test evidence gate")

    def test_same_name_joins_in_two_workspaces(self):
        for pane_id in ("w2:p1", "w1:p1"):
            with contextlib.redirect_stdout(io.StringIO()):
                radio.cmd_join(self.conn, join_args("reviewer", pane=pane_id))
        rows = self.conn.execute("SELECT workspace FROM handles ORDER BY workspace").fetchall()
        self.assertEqual([r["workspace"] for r in rows], ["w1", "w2"])

    def test_pre_scope_row_is_adopted(self):
        self.add_handle("reviewer", ref="herdr:w2:p1")  # pre-scope: workspace ''
        with contextlib.redirect_stdout(io.StringIO()):
            radio.cmd_join(self.conn, join_args("reviewer", pane="w2:p1"))
        rows = self.conn.execute("SELECT workspace, name FROM handles").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["workspace"], "w2")


class RestoreTest(RadioTestCase):
    """radio restore: verify the pane still belongs to the handle, then type
    the join/resume command into it; never creates layout."""

    def setUp(self):
        super().setUp()
        self._herdr = radio.herdr
        self._fetch = radio.fetch_pane
        self.addCleanup(setattr, radio, "herdr", self._herdr)
        self.addCleanup(setattr, radio, "fetch_pane", self._fetch)
        self.calls = []

        def fake_herdr(*args, **kwargs):
            self.calls.append(args)
            return subprocess.CompletedProcess(list(args), 0, stdout="", stderr="")

        radio.herdr = fake_herdr
        self.panes = {}
        radio.fetch_pane = lambda pane_id: self.panes.get(pane_id)
        self.add_handle("coder", ref="herdr:w2:p1", workspace="w2",
                        agent="codex", agent_session="sess-1")

    def restore(self, handle="coder"):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = radio.cmd_restore(self.conn, argparse.Namespace(handle=handle))
        return rc, buf.getvalue()

    def send_text_calls(self):
        return [call for call in self.calls if call[:2] == ("pane", "send-text")]

    def test_types_the_resume_command_into_the_pane(self):
        self.panes["w2:p1"] = {"label": "coder", "workspace_id": "w2"}
        rc, out = self.restore()
        self.assertEqual(rc, 0)
        calls = self.send_text_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], "w2:p1")
        self.assertIn("radio join coder --provider codex --resume", calls[0][3])
        self.assertIn(("pane", "send-keys", "w2:p1", "enter"), self.calls)
        self.assertIn("restoring coder", out)

    def test_starts_fresh_without_a_recorded_session(self):
        self.conn.execute("UPDATE handles SET agent_session=NULL WHERE name='coder'")
        self.conn.commit()
        self.panes["w2:p1"] = {"label": "coder", "workspace_id": "w2"}
        rc, out = self.restore()
        self.assertEqual(rc, 0)
        self.assertNotIn("--resume", self.send_text_calls()[0][3])
        self.assertIn("no recorded session", out)

    def test_already_running_is_a_noop(self):
        self.panes["w2:p1"] = {"label": "coder", "workspace_id": "w2", "agent": "codex"}
        rc, out = self.restore()
        self.assertEqual(rc, 0)
        self.assertIn("already running", out)
        self.assertEqual(self.send_text_calls(), [])

    def test_missing_pane_reports_what_to_do(self):
        with self.assertRaises(SystemExit) as ctx:
            self.restore()
        self.assertIn("radio join coder", str(ctx.exception))

    def test_foreign_agent_blocks(self):
        self.panes["w2:p1"] = {"label": "coder", "workspace_id": "w2", "agent": "claude"}
        with self.assertRaises(SystemExit):
            self.restore()

    def test_handle_without_provider(self):
        self.add_handle("plain", ref="herdr:w2:p2", workspace="w2")
        with self.assertRaises(SystemExit):
            self.restore("plain")


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
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(self.conn, *args)
        return buf.getvalue()

    def test_roster_shows_only_own_workspace(self):
        self.add_handle("alice", workspace="w2")
        self.add_handle("hede", workspace="w1")
        out = self.capture(radio.cmd_handles, argparse.Namespace(workspace=None))
        self.assertIn("alice", out)
        self.assertNotIn("hede", out)
        self.assertIn("Servers", out)

    def test_roster_filter_by_label(self):
        self.add_handle("alice", workspace="w2")
        self.add_handle("hede", workspace="w1")
        out = self.capture(radio.cmd_handles, argparse.Namespace(workspace="BoilerRoom"))
        self.assertIn("hede", out)
        self.assertNotIn("alice", out)

    def test_log_is_scoped(self):
        self.add_handle("alice", workspace="w2")
        self.add_handle("hede", workspace="w1")
        self.pm("alice", "alice", "own message", from_ws="w2", to_ws="w2")
        self.pm("hede", "hede", "other message", from_ws="w1", to_ws="w1")
        out = self.capture(radio.cmd_log, argparse.Namespace(limit=20))
        self.assertIn("own message", out)
        self.assertNotIn("other message", out)


class ScopeMigrationTest(unittest.TestCase):
    """A pre-scope ledger migrates to the scoped schema: rows survive as the
    unscoped namespace, the new columns exist, and the same name can then be
    joined again in another workspace."""

    def setUp(self):
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
        radio.STATE_DIR, radio.DB_PATH, radio.LOCK_PATH = saved

    def test_migration_preserves_rows_and_adds_scope(self):
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


class RetryBackoffTest(RadioTestCase):
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

    def tick(self, push_result=(True, None)):
        radio.fetch_pane = lambda pane_id: {"label": "bob"}

        def push(pane_id, text):
            self.push_calls.append(text)
            return push_result

        radio.push_to_pane = push
        return radio.relay_tick(self.conn)

    def test_errored_delivery_backs_off_then_retries(self):
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
        mid = self.pm("alice", "bob", "hi")
        self.tick()
        row = self.delivery_row(mid)
        self.assertEqual(row["status"], "delivered")
        self.assertIsNotNone(row["last_attempt_at"])  # attempts stamp on success too
        self.assertEqual(len(self.push_calls), 1)

    def test_fresh_first_ordering(self):
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
        mid = self.pm("alice", "bob", "hi")
        self.tick((False, "agent_blocked"))
        row = self.delivery_row(mid)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)
        self.assertIsNone(row["last_attempt_at"])


class CompareAndSwapTest(RadioTestCase):
    def test_ack_during_push_is_not_overwritten(self):
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
    def test_fingerprinting(self):
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


class TimeoutSplitTest(RadioTestCase):
    def setUp(self):
        super().setUp()
        self._subprocess = radio.subprocess
        self.addCleanup(self._restore)
        self.timeouts = []

    def _restore(self):
        radio.subprocess = self._subprocess

    def install_stub(self, returncode=0, stdout="{}"):
        import subprocess as real_subprocess
        timeouts = self.timeouts

        class Stub:
            TimeoutExpired = real_subprocess.TimeoutExpired
            CompletedProcess = real_subprocess.CompletedProcess

            @staticmethod
            def run(cmd, **kwargs):
                timeouts.append(kwargs.get("timeout"))
                return real_subprocess.CompletedProcess(
                    cmd, returncode, stdout=stdout, stderr=""
                )

        radio.subprocess = Stub

    def test_read_path_uses_read_timeout(self):
        self.install_stub(stdout='{"result": {"pane": {"label": "x"}}}')
        pane = radio.fetch_pane("w1:p1")
        self.assertEqual(pane, {"label": "x"})
        self.assertEqual(self.timeouts, [radio.HERDR_READ_TIMEOUT])

    def test_send_path_uses_send_timeout(self):
        self.install_stub(returncode=0)
        ok, error = radio.push_to_pane("w1:p1", "hi")
        self.assertEqual((ok, error), (True, None))
        self.assertEqual(self.timeouts, [radio.HERDR_SEND_TIMEOUT])


class Utf8StreamsTest(RadioTestCase):
    """Windows pipes default to cp1252; radio forces UTF-8 so typography in
    its output (·, ⚠, —) can never crash a command."""

    def test_reconfigures_real_streams_and_ignores_replaced_ones(self):
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
        calls = []
        saved_chdir = radio.os.chdir
        saved_tick = radio.relay_tick
        self.addCleanup(setattr, radio.os, "chdir", saved_chdir)
        self.addCleanup(setattr, radio, "relay_tick", saved_tick)
        radio.os.chdir = lambda path: calls.append(path)

        def stop(_conn):
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
        self.assertFalse(radio.script_changed("/nonexistent/radio", 1.0))
        self.assertTrue(radio.script_changed(str(REPO / "bin" / "radio"), 1.0))

    def test_on_disk_change_hands_over_to_a_successor(self):
        saved = (radio.script_changed, radio.subprocess.Popen, radio.relay_tick, radio.time.sleep)
        self.addCleanup(self._restore, saved)
        radio.script_changed = lambda script, mtime: True
        spawned = []

        class FakePopen:
            def __init__(self, argv, **kwargs):
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
        radio.script_changed, radio.subprocess.Popen, radio.relay_tick, radio.time.sleep = saved


class CrashSafeRelayTest(RadioTestCase):
    def test_tick_error_is_logged_and_the_daemon_survives(self):
        orig_tick = radio.relay_tick
        self.addCleanup(setattr, radio, "relay_tick", orig_tick)
        calls = []

        def flaky_tick(conn):
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
        self.assertEqual(self.run_pick("2\n")[0], 1)

    def test_enter_takes_the_default(self):
        self.assertEqual(self.run_pick("\n", default=2)[0], 2)

    def test_q_cancels(self):
        self.assertEqual(self.run_pick("q\n")[0], None)

    def test_closed_stdin_cancels(self):
        self.assertEqual(self.run_pick("")[0], None)

    def test_invalid_line_reprompts(self):
        idx, out = self.run_pick("nope\n3\n")
        self.assertEqual(idx, 2)
        self.assertIn("invalid choice", out)

    def test_choose_dispatches_when_termios_is_missing(self):
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
        saved = radio.fcntl, radio.msvcrt
        self.addCleanup(self._restore, saved)
        radio.fcntl, radio.msvcrt = None, fake

    @staticmethod
    def _restore(saved):
        radio.fcntl, radio.msvcrt = saved

    def test_posix_second_lock_is_refused(self):
        lock = radio.STATE_DIR / "relay.lock"
        first, second = open(lock, "a+"), open(lock, "a+")
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        self.assertTrue(radio.relay_lock(first))
        self.assertFalse(radio.relay_lock(second))

    def test_windows_fallback_locks_one_byte(self):
        calls = []

        class FakeMsvcrt:
            LK_NBLCK = 2

            @staticmethod
            def locking(fd, mode, size):
                calls.append((mode, size))

        self._force_windows_lock(FakeMsvcrt)
        with open(radio.STATE_DIR / "relay.lock", "a+") as fd:
            self.assertTrue(radio.relay_lock(fd))
            self.assertEqual(calls, [(FakeMsvcrt.LK_NBLCK, 1)])
            fd.seek(0)
            self.assertEqual(fd.read(1), "1")  # region kept non-empty

    def test_windows_fallback_reports_contention(self):
        class BusyMsvcrt:
            LK_NBLCK = 2

            @staticmethod
            def locking(fd, mode, size):
                raise OSError("lock held")

        self._force_windows_lock(BusyMsvcrt)
        with open(radio.STATE_DIR / "relay.lock", "a+") as fd:
            self.assertFalse(radio.relay_lock(fd))


class AgentLaunchArgvTest(unittest.TestCase):
    """agent_launch_argv: POSIX execs the resolved binary; Windows npm-style
    .cmd/.bat shims cannot be CreateProcess'd and get wrapped in cmd /c."""

    def setUp(self):
        self._which = radio.shutil.which
        self._name = radio.os.name
        self.addCleanup(self._restore)

    def _restore(self):
        radio.shutil.which = self._which
        radio.os.name = self._name

    def test_posix_resolves_the_binary(self):
        radio.shutil.which = lambda cmd: f"/usr/local/bin/{cmd}"
        self.assertEqual(
            radio.agent_launch_argv(["claude", "--name", "x"]),
            ["/usr/local/bin/claude", "--name", "x"],
        )

    def test_windows_cmd_shim_is_wrapped(self):
        radio.os.name = "nt"
        radio.shutil.which = lambda cmd: r"C:\npm\claude.cmd"
        self.assertEqual(
            radio.agent_launch_argv(["claude", "--name", "x"]),
            ["cmd.exe", "/c", r"C:\npm\claude.cmd", "--name", "x"],
        )

    def test_windows_exe_is_not_wrapped(self):
        radio.os.name = "nt"
        radio.shutil.which = lambda cmd: r"C:\tools\codex.exe"
        self.assertEqual(radio.agent_launch_argv(["codex"]), [r"C:\tools\codex.exe"])


class ExecOrWaitTest(unittest.TestCase):
    """exec_or_wait: POSIX execs in place; Windows waits on a child, because
    the CRT's exec emulation returns the shell prompt while the target runs."""

    def setUp(self):
        self._name = radio.os.name
        self._run = radio.subprocess.run
        self._execvpe = radio.os.execvpe
        self.addCleanup(self._restore)

    def _restore(self):
        radio.os.name = self._name
        radio.subprocess.run = self._run
        radio.os.execvpe = self._execvpe

    def test_posix_execs_in_place(self):
        seen = {}

        def fake_execvpe(path, argv, env):
            seen["argv"] = argv
            raise RuntimeError("exec")

        radio.os.execvpe = fake_execvpe
        with self.assertRaises(RuntimeError):
            radio.exec_or_wait(["claude", "--name", "x"], {})
        self.assertEqual(seen["argv"], ["claude", "--name", "x"])

    def test_windows_waits_and_returns_the_exit_code(self):
        radio.os.name = "nt"
        radio.subprocess.run = lambda command, env=None: subprocess.CompletedProcess(command, 3)
        self.assertEqual(radio.exec_or_wait(["cmd.exe", "/c", "claude.cmd"], {}), 3)

    def test_windows_command_line_plain_binary(self):
        self.assertEqual(
            radio.windows_command_line([r"C:\tools\codex.exe", "--resume", "sid"]),
            r"C:\tools\codex.exe --resume sid",
        )

    def test_windows_command_line_quotes_space_paths(self):
        # cmd /c strips a lone outer pair; the extra pair keeps the path whole.
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
        super().setUp()
        self.win_shim = load_bin_script("radio_win_shim", "win-shim.py")
        self.link_dir = radio.STATE_DIR / "link-bin"

    def test_shim_text_is_stable_and_bakes_no_root(self):
        text = self.win_shim.shim_text()
        self.assertTrue(text.startswith(self.win_shim.SHIM_MARKER))
        self.assertIn(self.win_shim.ROOT_FILE, text)
        self.assertIn(self.win_shim.RESOLVER_FILE, text)
        self.assertNotIn("plugins\\radio-", text)  # no baked plugin path

    def test_resolver_text_queries_herdr(self):
        text = self.win_shim.resolver_text()
        self.assertIn(self.win_shim.RESOLVER_MARKER, text)
        self.assertIn("plugin_root", text)
        self.assertIn('"radio"', text)

    def test_ensure_shim_writes_launcher_resolver_and_cache(self):
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

    def test_foreign_radio_cmd_is_left_alone(self):
        self.link_dir.mkdir(parents=True, exist_ok=True)
        shim = self.link_dir / "radio.cmd"
        shim.write_text("@echo off\necho someone else's radio\n", encoding="utf-8")
        msg = self.win_shim.ensure_shim(Path(r"C:\plugins\radio-a"), self.link_dir)
        self.assertIn("left alone", msg)
        self.assertIn("someone else's radio", shim.read_text(encoding="utf-8"))

    def test_append_path_entry_appends_and_dedupes(self):
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
        self.assertIsNone(self.win_shim.ensure_user_path(self.link_dir))

    def test_foreign_resolver_is_left_alone(self):
        self.link_dir.mkdir(parents=True, exist_ok=True)
        resolver = self.link_dir / self.win_shim.RESOLVER_FILE
        resolver.write_text("print('someone else')\n", encoding="utf-8")
        msg = self.win_shim.ensure_shim(Path(r"C:\plugins\radio-a"), self.link_dir)
        self.assertIn("left alone", msg)
        self.assertIn("someone else", resolver.read_text(encoding="utf-8"))

    def test_path_hint_only_when_missing(self):
        saved = os.environ.get("PATH")
        self.addCleanup(self._restore_path, saved)
        os.environ["PATH"] = str(self.link_dir)
        self.assertIsNone(self.win_shim.path_hint(self.link_dir))
        os.environ["PATH"] = "/nowhere"
        hint = self.win_shim.path_hint(self.link_dir)
        self.assertIn(str(self.link_dir), hint)

    @staticmethod
    def _restore_path(saved):
        if saved is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = saved


class ViewPythonTest(unittest.TestCase):
    """run-view.py: prefer the state-dir venv interpreter (per-platform
    layout), fall back to the launcher's own interpreter."""

    def setUp(self):
        self.run_view = load_bin_script("radio_run_view", "run-view.py")

    def test_prefers_posix_venv(self):
        with tempfile.TemporaryDirectory() as tmp:
            venv = Path(tmp) / "venv"
            venv_python = venv / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("")
            self.assertEqual(self.run_view.view_python(venv), venv_python)

    def test_prefers_windows_venv(self):
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
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                self.run_view.view_python(Path(tmp) / "venv"), Path(sys.executable)
            )

    def test_state_dir_honours_radio_home(self):
        saved = os.environ.get("RADIO_HOME")
        self.addCleanup(self._restore_env, saved)
        os.environ["RADIO_HOME"] = "/tmp/radio-home-test"
        self.assertEqual(self.run_view.state_dir(), Path("/tmp/radio-home-test"))

    def test_detach_cwd_leaves_the_plugin_dir(self):
        saved_home = os.environ.get("RADIO_HOME")
        self.addCleanup(self._restore_env, saved_home)
        os.environ["RADIO_HOME"] = "/tmp/radio-home-test"
        calls = []
        saved_chdir = self.run_view.os.chdir
        self.addCleanup(setattr, self.run_view.os, "chdir", saved_chdir)
        self.run_view.os.chdir = lambda path: calls.append(path)
        self.run_view.detach_cwd()
        self.assertEqual(calls, [Path("/tmp/radio-home-test")])

    @staticmethod
    def _restore_env(saved):
        if saved is None:
            os.environ.pop("RADIO_HOME", None)
        else:
            os.environ["RADIO_HOME"] = saved


class WorkspaceCreatedHookTest(unittest.TestCase):
    """The workspace.created hook reads the event payload defensively and
    opens the platform-correct Radio view pane without stealing focus."""

    def setUp(self):
        self.hook = load_bin_script("radio_workspace_hook", "workspace-created.py")
        for key in ("HERDR_PLUGIN_EVENT_JSON", "HERDR_WORKSPACE_ID", "HERDR_BIN_PATH"):
            saved = os.environ.pop(key, None)
            if saved is not None:
                self.addCleanup(os.environ.__setitem__, key, saved)

    def test_event_workspace_variants(self):
        self.assertEqual(self.hook.event_workspace('{"workspace":{"workspace_id":"w7"}}'), "w7")
        self.assertEqual(self.hook.event_workspace('{"workspace_id":"wA","x":1}'), "wA")
        self.assertEqual(self.hook.event_workspace('{"items":[{"workspace_id":"w9"}]}'), "w9")
        self.assertEqual(self.hook.event_workspace("not json"), "")
        self.assertEqual(self.hook.event_workspace("{}"), "")

    def test_main_opens_the_view_in_the_new_workspace(self):
        calls = []

        class FakeSubprocess:
            TimeoutExpired = subprocess.TimeoutExpired

            @staticmethod
            def run(argv, **kwargs):
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
        self.hook.main()  # no payload, no env: returns quietly


class WindowsScriptsTest(unittest.TestCase):
    """The Windows hook bodies must import and no-op cleanly off Windows."""

    def test_noops_off_windows(self):
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
        self.assertFalse(radio_view.sidebar_visible(radio_view.NARROW_COLS - 1, None))
        self.assertTrue(radio_view.sidebar_visible(radio_view.NARROW_COLS, None))
        self.assertTrue(radio_view.sidebar_visible(200, None))

    def test_forced_overrides_width(self):
        self.assertTrue(radio_view.sidebar_visible(40, True))
        self.assertFalse(radio_view.sidebar_visible(200, False))


@unittest.skipUnless(HAS_VIEW, "view deps (textual) not installed")
class CompactRosterTest(RadioTestCase):
    """Compact header roster: live handles inline, the rest as +N."""

    def test_live_inline_dead_counted(self):
        self.add_handle("kimi", ref="herdr:1-1")
        self.add_handle("codex", ref="manual")  # no pane binding -> pull dot
        handles = self.conn.execute("SELECT * FROM handles ORDER BY name").fetchall()
        states = {"1-1": {"label": "kimi", "agent": None, "agent_status": "idle"}}
        roster = radio_view.compact_roster(handles, states)
        self.assertEqual(roster.plain, "●kimi +1")

    def test_all_dead_is_only_the_count(self):
        self.add_handle("kimi", ref="manual")
        self.add_handle("codex", ref="herdr:9-9")  # pane gone -> ✗
        handles = self.conn.execute("SELECT * FROM handles ORDER BY name").fetchall()
        roster = radio_view.compact_roster(handles, {})
        self.assertEqual(roster.plain, " +2")


@unittest.skipUnless(HAS_VIEW, "view deps (textual) not installed")
class ViewScopeTest(RadioTestCase):
    """The view starts scoped to its pane's workspace (`a` toggles all): the
    scope comes from the herdr env and the query builders filter handles and
    messages without leaking another workspace's traffic."""

    def test_scope_from_herdr_env(self):
        os.environ["HERDR_ENV"] = "1"
        os.environ["HERDR_WORKSPACE_ID"] = "w2"
        self.addCleanup(os.environ.pop, "HERDR_ENV", None)
        self.addCleanup(os.environ.pop, "HERDR_WORKSPACE_ID", None)
        self.assertEqual(radio_view.workspace_scope(), "w2")
        os.environ.pop("HERDR_ENV")
        self.assertEqual(radio_view.workspace_scope(), "")

    def test_handles_query_scopes(self):
        sql, params = radio_view.handles_query("w2", False)
        self.assertIn("WHERE workspace = ?", sql)
        self.assertEqual(params, ("w2",))
        sql, params = radio_view.handles_query("w2", True)
        self.assertNotIn("WHERE workspace", sql)
        self.assertEqual(params, ())

    def test_messages_query_scopes(self):
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
        self.assertEqual(fetched, ["w2:p1"])
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
        return json.dumps(
            {"result": {"panes": [{"pane_id": p, "label": "x"} for p in pane_ids]}}
        )

    def setUp(self):
        super().setUp()
        self._herdr = radio_view.herdr
        self.addCleanup(self._restore)
        self.calls = []

    def _restore(self):
        radio_view.herdr = self._herdr

    def install(self, routes):
        """Script radio_view.herdr: argv tuple -> (stdout, rc), or None (binary gone)."""
        def fake(*args):
            self.calls.append(args)
            route = routes.get(tuple(args), ("{}", 1))
            if route is None:
                return None
            stdout, rc = route
            return subprocess.CompletedProcess(list(args), rc, stdout=stdout, stderr="")

        radio_view.herdr = fake

    def test_merges_all_workspaces(self):
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
        self.install({
            ("workspace", "list"): (self.WS_TWO, 0),
            ("pane", "list", "--workspace", "w1"): (self._panes("w1:p1"), 0),
            ("pane", "list", "--workspace", "w2"): ("", 1),  # closed mid-scan
        })
        states = radio_view.pane_states()
        self.assertEqual(set(states), {"w1:p1"})

    def test_dot_for_handle_in_non_default_workspace(self):
        self.add_handle("bob", ref="herdr:w2:p3")
        row = self.conn.execute("SELECT * FROM handles WHERE name='bob'").fetchone()
        states = {"w2:p3": {"label": "bob", "agent_status": "idle"}}
        dot, note = radio_view.dot_for(row, states)
        self.assertEqual((dot.plain, note), ("●", "idle"))

    def test_duplicate_labels_across_workspaces_stay_keyed_by_pane_id(self):
        # Two workspaces may hold a pane with the same label: dot_for looks
        # the pane up by its workspace-prefixed id; the label is only the
        # identity check, never the lookup key.
        self.add_handle("bob", ref="herdr:w2:p1", agent="claude")
        row = self.conn.execute("SELECT * FROM handles WHERE name='bob'").fetchone()
        states = {
            "w1:p1": {"label": "bob", "agent": "claude", "agent_status": "working"},
            "w2:p1": {"label": "bob", "agent": "claude", "agent_status": "idle"},
        }
        dot, note = radio_view.dot_for(row, states)
        self.assertEqual((dot.plain, note), ("●", "idle"))  # w2's pane, not w1's


if __name__ == "__main__":
    unittest.main()
