#!/usr/bin/env python3
"""Frequency isolation and migration acceptance tests.

Only temporary SQLite ledgers are used. Herdr, provider lookup, process launch,
and pane delivery are patched for every test; this module does not import or
run the installation/startup tests in test_radio.py.
"""

import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parent.parent
_IMPORT_HOME = tempfile.TemporaryDirectory(prefix="radio-frequency-import-")
with patch.dict(os.environ, {"RADIO_HOME": _IMPORT_HOME.name}):
    _loader = importlib.machinery.SourceFileLoader(
        "radio_frequencies_under_test", str(REPO / "bin" / "radio")
    )
    _spec = importlib.util.spec_from_loader(_loader.name, _loader)
    radio = importlib.util.module_from_spec(_spec)
    _loader.exec_module(radio)


def join_args(handle="alice", pane="w1:p1", **overrides):
    """Build the join namespace the frequency tests use, with overrides."""
    values = dict(handle=handle, pane=pane, provider=None, new=False,
                  resume=False, model=None, role=None, account=None,
                  no_launch=True, frequency=None, workspace_frequency=False)
    values.update(overrides)
    return argparse.Namespace(**values)


class FrequencyCase(unittest.TestCase):
    """Shared fixture: a temp ledger with herdr, pane lookup, push and process launch stubbed."""

    def setUp(self):
        """Point radio at a temp ledger, strip ambient env, and stub every herdr and process
        boundary."""
        self.tmp = tempfile.TemporaryDirectory(prefix="radio-frequency-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ))
        for key in tuple(os.environ):
            if key.startswith("HERDR_") or key.startswith("RADIO_"):
                os.environ.pop(key)
        os.environ["RADIO_HOME"] = str(self.root)
        self.stack.enter_context(patch.multiple(
            radio, STATE_DIR=self.root, DB_PATH=self.root / "radio.db",
            LOCK_PATH=self.root / "relay.lock"))
        self.conn = radio.connect()
        self.addCleanup(self.conn.close)
        self.panes = {}
        self.herdr_calls = []
        self.pushed = []
        self.stack.enter_context(patch.object(radio, "herdr", self.fake_herdr))
        self.stack.enter_context(patch.object(radio, "herdr_json", self.fake_herdr_json))
        self.stack.enter_context(patch.object(
            radio, "fetch_pane", lambda pane_id: self.panes.get(pane_id)))
        self.stack.enter_context(patch.object(
            radio, "pane_exists", lambda pane_id: pane_id in self.panes))
        self.stack.enter_context(patch.object(radio, "push_to_pane", self.fake_push))
        self.session_search = self.stack.enter_context(patch.object(
            radio, "find_named_session", return_value=None))
        # These guards make accidental integration activity a test failure.
        for owner, name in ((radio.subprocess, "run"), (radio.subprocess, "Popen"),
                            (radio, "exec_or_wait"), (radio.os, "execvpe")):
            self.stack.enter_context(patch.object(
                owner, name, side_effect=AssertionError("real process launch in frequency test")))

    def fake_herdr(self, *args, **kwargs):
        """Record herdr calls; a pane rename updates the fake pane label."""
        self.herdr_calls.append(args)
        if args[:2] == ("pane", "rename"):
            self.panes[args[2]]["label"] = args[3]
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def fake_herdr_json(self, *args):
        """Answer workspace list with two labeled workspaces."""
        if args[:2] == ("workspace", "list"):
            return {"result": {"workspaces": [
                {"workspace_id": "w1", "label": "Project One"},
                {"workspace_id": "w2", "label": "Project Two"},
            ]}}
        return {}

    def fake_push(self, pane_id, text):
        """Record a delivery and report success."""
        self.pushed.append((pane_id, text))
        return True, None

    def pane(self, pane_id="w1:p1", name="alice", scope="w1", **fields):
        """Register a fake pane; a named scope labels it name@frequency."""
        workspace = pane_id.split(":", 1)[0]
        label = name + "@" + scope[5:] if scope.startswith("freq.") else name
        self.panes[pane_id] = dict(label=label, workspace_id=workspace,
                                   agent_status="idle", **fields)
        return self.panes[pane_id]

    def enter(self, pane_id="w1:p1"):
        """Pretend the command runs inside the given herdr pane."""
        os.environ.update(HERDR_ENV="1", HERDR_PANE_ID=pane_id,
                          HERDR_WORKSPACE_ID=pane_id.split(":", 1)[0])

    def add_handle(self, name="alice", scope="freq.team", pane_id="w1:p1",
                   physical=None, agent=None, session=None, role=None, account=None):
        """Insert a handle row and its matching fake pane."""
        physical = (pane_id.split(":", 1)[0] if pane_id else "") if physical is None else physical
        self.conn.execute(
            """INSERT INTO handles(workspace,name,session_ref,pane_workspace,
               agent,agent_session,role,account,created_at,last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (scope, name, "herdr:" + pane_id if pane_id else "manual", physical,
             agent, session, role, account, radio.now(), radio.now()))
        self.conn.commit()
        if pane_id:
            fields = {"agent": agent} if agent else {}
            self.pane(pane_id, name, scope, **fields)

    def row(self, scope="freq.team", name="alice"):
        """Fetch the handle row for a scope and name."""
        return self.conn.execute(
            "SELECT * FROM handles WHERE workspace=? AND name=?", (scope, name)).fetchone()

    def pm(self, to="bob", text="frequency hello", sender=None):
        """Send a PM as cmd_pm would, with stdout captured."""
        return self.capture(radio.cmd_pm, argparse.Namespace(
            sender=sender, to=to, text=[text], ref=None, reply_required=False))

    def seed_message(self, scope, text, to="alice", sender="sender"):
        """Record a message and enqueue its delivery directly."""
        mid = radio.record_message(self.conn, "pm", sender, text,
                                   to_handle=to, from_ws=scope, to_ws=scope)
        radio.enqueue_delivery(self.conn, mid, to, scope)
        self.conn.commit()
        return mid

    def delivery(self, mid):
        """Fetch the delivery row for a message id."""
        return self.conn.execute(
            "SELECT * FROM deliveries WHERE message_id=?", (mid,)).fetchone()

    def capture(self, fn, args):
        """Run a command with stdout captured and return the printed text."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(self.conn, args)
        return buf.getvalue()

    def assert_resolution_denied(self, spec, scope):
        """A denied resolution may return None or exit; both count as denied."""
        try:
            result = radio.resolve_handle(self.conn, spec, scope)
        except SystemExit:
            return
        self.assertIsNone(result, (spec, scope))


class FrequencyIdentityTest(FrequencyCase):
    """Frequency scopes: slug rules, reserved prefix, label/workspace split, no cross-scope
    resolution."""

    def test_slug_normalizes_and_cannot_collide_with_workspace_scope(self):
        """Names are slugged under the reserved freq. prefix; invalid names exit."""
        self.assertEqual(radio.frequency_scope("Team_2-North"), "freq.team_2-north")
        self.assertEqual(radio.frequency_scope("w1"), "freq.w1")
        self.assertTrue(radio.is_frequency("freq.team"))
        self.assertFalse(radio.is_frequency("w1"))
        for name in ("", "a b", "a:b", "a.b", "../team", "_team", "-team", "team]", "équipe"):
            with self.subTest(name=name), self.assertRaises((SystemExit, ValueError)):
                radio.frequency_scope(name)

    def test_label_and_physical_workspace_are_distinct_from_logical_scope(self):
        """pane_label and scope_workspace keep label, physical pane and logical scope distinct."""
        self.assertEqual(radio.pane_label("alice", "freq.team"), "alice@team")
        self.assertEqual(radio.pane_label("alice", "w1"), "alice")
        self.add_handle(physical="w1")
        self.assertEqual(radio.scope_workspace(self.row()), "w1")
        self.assertEqual(radio.scope_workspace({"workspace": "w2"}), "w2")

    def test_bound_pane_wins_over_stale_frequency_environment(self):
        """A pane binding outranks a stale RADIO_FREQUENCY/RADIO_HANDLE environment."""
        self.add_handle()
        self.enter()
        os.environ.update(RADIO_FREQUENCY="other", RADIO_HANDLE="stale")
        self.assertEqual(radio.current_scope(self.conn), "freq.team")

    def test_unbound_pane_uses_workspace_and_ignores_frequency_environment(self):
        """An unbound pane stays on its workspace and ignores the frequency env."""
        self.enter()
        os.environ["RADIO_FREQUENCY"] = "team"
        self.assertEqual(radio.current_scope(self.conn), "w1")

    def test_multiple_bindings_for_one_pane_fail_closed(self):
        """Two bindings on one pane exit instead of guessing."""
        self.add_handle(scope="freq.one")
        self.add_handle(scope="freq.two")
        self.enter()
        with self.assertRaises(SystemExit):
            radio.current_scope(self.conn)

    def test_named_scope_has_no_global_handle_fallback(self):
        """A named frequency never reaches an unscoped global handle."""
        self.add_handle("globalbot", scope="", pane_id=None)
        self.assert_resolution_denied("globalbot", "freq.team")
        self.assertEqual(radio.resolve_handle(self.conn, "globalbot", "w1")["workspace"], "")

    def test_qualified_names_cannot_escape_or_enter_named_frequency(self):
        """Qualified names cannot cross into or out of a named frequency."""
        self.add_handle("bob", scope="freq.team", pane_id="w1:p2")
        self.add_handle("bob", scope="freq.other", pane_id="w2:p2")
        self.add_handle("bob", scope="w1", pane_id=None)
        for spec, scope in (("freq.other:bob", "freq.team"),
                            ("w1:bob", "freq.team"),
                            ("freq.team:bob", "w1"),
                            ("freq.team:bob", "")):
            with self.subTest(spec=spec, scope=scope):
                self.assert_resolution_denied(spec, scope)


class FrequencyJoinTest(FrequencyCase):
    """join: default vs named binding, retuning, resume scope, and the concurrent-
    registration race."""

    def test_plain_join_keeps_default_workspace_behavior(self):
        """A plain join records the workspace as both scope and physical workspace."""
        self.pane()
        self.capture(radio.cmd_join, join_args())
        row = self.row("w1")
        self.assertEqual((row["workspace"], row["pane_workspace"]), ("w1", "w1"))
        self.assertEqual(self.panes["w1:p1"]["label"], "alice")

    def test_named_join_records_physical_workspace_and_frequency_label(self):
        """A named join records freq.<name> with the pane's workspace and labels the pane
        name@frequency."""
        self.pane()
        self.capture(radio.cmd_join, join_args(frequency="Team"))
        row = self.row()
        self.assertEqual((row["workspace"], row["pane_workspace"]), ("freq.team", "w1"))
        self.assertEqual(self.panes["w1:p1"]["label"], "alice@team")

    def test_flagless_rejoin_preserves_bound_frequency(self):
        """A flagless rejoin keeps the pane's current frequency."""
        self.add_handle()
        self.enter()
        self.capture(radio.cmd_join, join_args())
        self.assertIsNone(self.row("w1"))
        self.assertEqual(self.row()["session_ref"], "herdr:w1:p1")

    def test_same_handle_can_join_separate_frequencies_in_one_workspace(self):
        """One handle can bind twice, once per frequency, in one workspace."""
        self.pane("w1:p1")
        self.pane("w1:p2")
        self.capture(radio.cmd_join, join_args(frequency="one"))
        self.capture(radio.cmd_join, join_args(pane="w1:p2", frequency="two"))
        self.assertEqual(self.row("freq.one")["session_ref"], "herdr:w1:p1")
        self.assertEqual(self.row("freq.two")["session_ref"], "herdr:w1:p2")

    def test_retuning_detaches_old_binding_and_defers_queued_messages(self):
        """Retuning detaches the old binding, keeps its session, and leaves its queued mail
        for pull."""
        self.add_handle(scope="freq.old", session="old-session")
        self.enter()
        mid = self.seed_message("freq.old", "old-private-message")
        self.capture(radio.cmd_join, join_args(frequency="new"))
        self.assertEqual(self.row("freq.old")["session_ref"], "manual")
        self.assertEqual(self.row("freq.old")["agent_session"], "old-session")
        self.assertEqual(self.row("freq.new")["session_ref"], "herdr:w1:p1")
        self.assertIsNone(self.row("freq.new")["agent_session"])
        self.assertEqual((self.delivery(mid)["status"], self.delivery(mid)["last_error"]),
                         ("pull", "frequency changed"))
        radio.relay_tick(self.conn)
        self.assertEqual(self.pushed, [])
        self.assertEqual(self.delivery(mid)["last_error"], "frequency changed")

    def test_retuning_running_agent_is_rejected_without_ledger_changes(self):
        """Retuning a live agent exits and changes nothing."""
        self.add_handle(scope="freq.old", agent="codex", session="active-session")
        self.enter()
        mid = self.seed_message("freq.old", "queued-private-message")
        with self.assertRaises(SystemExit):
            self.capture(radio.cmd_join, join_args(frequency="new"))
        self.assertIsNone(self.row("freq.new"))
        self.assertEqual(self.row("freq.old")["session_ref"], "herdr:w1:p1")
        self.assertEqual(self.delivery(mid)["status"], "pending")
        self.assertEqual(self.panes["w1:p1"]["label"], "alice@old")

    def test_explicit_workspace_frequency_returns_to_default_scope(self):
        """An explicit workspace selection returns the pane to its default scope."""
        self.add_handle()
        self.enter()
        self.capture(radio.cmd_join, join_args(workspace_frequency=True))
        self.assertEqual(self.row()["session_ref"], "manual")
        self.assertEqual(self.row("w1")["session_ref"], "herdr:w1:p1")
        self.assertEqual(self.panes["w1:p1"]["label"], "alice")

    def test_new_frequency_does_not_adopt_stale_pane_session(self):
        """A new frequency never adopts a stale pane session."""
        self.pane(agent_session={"value": "unrelated-session"})
        self.capture(radio.cmd_join, join_args(frequency="team"))
        self.assertIsNone(self.row()["agent_session"])

    def test_frequency_resume_uses_only_recorded_session(self):
        """Resume inside a frequency uses only the recorded session, never a disk search."""
        self.add_handle(agent="codex", session="recorded-session")
        self.panes["w1:p1"].pop("agent")
        self.enter()
        self.capture(radio.cmd_join, join_args(provider="codex", resume=True))
        self.assertEqual(self.row()["agent_session"], "recorded-session")
        self.session_search.assert_not_called()

    def test_frequency_resume_without_recorded_session_never_searches_other_sessions(self):
        """With no recorded session, resume exits instead of searching other sessions."""
        self.pane()
        self.session_search.return_value = {"id": "foreign-session", "cwd": "/elsewhere"}
        with self.assertRaises(SystemExit):
            self.capture(radio.cmd_join, join_args(frequency="team", provider="codex", resume=True))
        self.session_search.assert_not_called()

    def test_outside_join_explicit_pane_inherits_target_frequency(self):
        """Joining an explicit pane from outside adopts that pane's frequency."""
        self.add_handle("target", scope="freq.target", pane_id="w2:p1")
        self.capture(radio.cmd_join, join_args(handle=None, pane="w2:p1"))
        self.assertEqual(self.row("freq.target", "target")["session_ref"], "herdr:w2:p1")
        self.assertIsNone(self.row("w2", "target"))
        self.assertEqual(self.panes["w2:p1"]["label"], "target@target")

    def test_named_caller_joining_clean_explicit_pane_uses_target_workspace(self):
        """A named caller joining a clean pane binds it to the pane's workspace."""
        self.add_handle(scope="freq.caller")
        self.pane("w2:p2", "target", "w2")
        self.enter()
        self.capture(radio.cmd_join, join_args(handle="target", pane="w2:p2"))
        self.assertEqual(self.row("w2", "target")["session_ref"], "herdr:w2:p2")
        self.assertIsNone(self.row("freq.caller", "target"))
        self.assertEqual(self.row("freq.caller")["session_ref"], "herdr:w1:p1")

    def test_named_caller_joining_bound_explicit_pane_preserves_target_frequency(self):
        """A named caller joining a bound pane preserves the target's frequency."""
        self.add_handle(scope="freq.caller")
        self.add_handle("target", scope="freq.target", pane_id="w2:p2")
        self.enter()
        self.capture(radio.cmd_join, join_args(handle=None, pane="w2:p2"))
        self.assertEqual(self.row("freq.target", "target")["session_ref"], "herdr:w2:p2")
        self.assertIsNone(self.row("freq.caller", "target"))
        self.assertEqual(self.row("freq.caller")["session_ref"], "herdr:w1:p1")

    def test_concurrent_named_registration_cannot_be_stolen_before_join_lock(self):
        """A competing registration committed before the join lock is not stolen."""
        self.pane()
        self.pane("w2:p9", "alice", "freq.team", agent="codex")
        db = self.root / "radio.db"

        class RegisterBeforeLock:
            """Commit a competing join after optimistic lookup, before locking."""

            def __init__(self, connection):
                """Keep the wrapped connection and the one-shot trigger flag."""
                self.connection = connection
                self.triggered = False

            def __getattr__(self, name):
                """Delegate every other attribute to the wrapped connection."""
                return getattr(self.connection, name)

            def execute(self, sql, *args, **kwargs):
                """Commit a competing registration just before the join's BEGIN IMMEDIATE."""
                if sql.strip().upper() == "BEGIN IMMEDIATE" and not self.triggered:
                    self.triggered = True
                    with contextlib.closing(sqlite3.connect(db)) as competing:
                        competing.execute(
                            """INSERT INTO handles(workspace,name,session_ref,pane_workspace,
                               agent,agent_session,created_at,last_seen) VALUES (?,?,?,?,?,?,?,?)""",
                            ("freq.team", "alice", "herdr:w2:p9", "w2", "codex",
                             "competing-active-session", radio.now(), radio.now()))
                        competing.commit()
                return self.connection.execute(sql, *args, **kwargs)

        connection = RegisterBeforeLock(self.conn)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            radio.cmd_join(connection, join_args(frequency="team"))
        self.assertTrue(connection.triggered, "test must insert during the join race window")
        winner = self.row()
        self.assertEqual((winner["session_ref"], winner["agent_session"]),
                         ("herdr:w2:p9", "competing-active-session"))
        self.assertEqual(self.panes["w1:p1"]["label"], "alice")

    def test_named_resume_uses_session_backfilled_from_matching_live_agent(self):
        """Resume accepts a session backfilled from the matching live agent."""
        self.add_handle(agent="codex")
        self.panes["w1:p1"]["agent_session"] = {"value": "backfilled-live-session"}
        self.enter()
        self.capture(radio.cmd_join, join_args(provider="codex", resume=True))
        self.assertEqual(self.row()["agent_session"], "backfilled-live-session")
        self.session_search.assert_not_called()

    def test_returning_to_workspace_cannot_resume_a_disk_search_match(self):
        """Returning to the workspace never resumes an unrelated disk-search match."""
        self.add_handle(agent="codex", session="named-session")
        self.panes["w1:p1"].pop("agent")
        self.enter()
        self.session_search.return_value = {"id": "unrelated-workspace-session", "cwd": "/elsewhere"}
        with self.assertRaises(SystemExit):
            self.capture(radio.cmd_join, join_args(
                provider="codex", resume=True, workspace_frequency=True))
        self.session_search.assert_not_called()
        self.assertIsNone(self.row("w1"))
        self.assertEqual(self.row()["session_ref"], "herdr:w1:p1")
        self.assertEqual(self.row()["agent_session"], "named-session")

    def test_returning_to_workspace_may_resume_exact_destination_record(self):
        """Returning to the workspace may resume the exact destination record."""
        self.add_handle(agent="codex", session="named-session")
        self.panes["w1:p1"].pop("agent")
        self.add_handle(scope="w1", pane_id=None, physical="w1",
                        agent="codex", session="workspace-recorded-session")
        self.enter()
        self.capture(radio.cmd_join, join_args(
            provider="codex", resume=True, workspace_frequency=True))
        self.assertEqual(self.row()["session_ref"], "manual")
        self.assertEqual(self.row("w1")["agent_session"], "workspace-recorded-session")
        self.session_search.assert_not_called()

    def test_live_agent_cannot_change_handle_within_its_named_frequency(self):
        """A live agent's handle cannot be renamed within its frequency."""
        self.add_handle(agent="codex", session="active-session")
        self.enter()
        mid = self.seed_message("freq.team", "private-for-alice")
        with self.assertRaises(SystemExit):
            self.capture(radio.cmd_join, join_args(handle="replacement"))
        self.assertIsNone(self.row("freq.team", "replacement"))
        self.assertEqual(self.row()["session_ref"], "herdr:w1:p1")
        self.assertEqual(self.row()["agent_session"], "active-session")
        self.assertEqual(self.delivery(mid)["status"], "pending")
        self.assertEqual(self.panes["w1:p1"]["label"], "alice@team")

    def test_live_agent_identity_preserving_named_rejoin_is_allowed(self):
        """An identity-preserving rejoin of a live agent is allowed."""
        self.add_handle(agent="codex", session="active-session")
        self.enter()
        self.capture(radio.cmd_join, join_args(provider="codex"))
        self.assertEqual(self.row()["session_ref"], "herdr:w1:p1")
        self.assertEqual(self.row()["agent_session"], "active-session")
        self.assertEqual(self.panes["w1:p1"]["label"], "alice@team")


class FrequencyMessagingTest(FrequencyCase):
    """Messaging: routing inside one frequency, unreachable neighbours, and reads that never
    leak across."""

    def test_same_frequency_routes_across_physical_workspaces(self):
        """One frequency routes across physical workspaces and delivers."""
        self.add_handle()
        self.add_handle("bob", pane_id="w2:p2")
        self.enter()
        self.pm()
        message = self.conn.execute("SELECT * FROM messages").fetchone()
        self.assertEqual((message["from_ws"], message["to_ws"]), ("freq.team", "freq.team"))
        radio.relay_tick(self.conn)
        self.assertEqual([p for p, _ in self.pushed], ["w2:p2"])
        self.assertEqual(self.delivery(message["id"])["status"], "delivered")

    def test_other_frequency_in_same_workspace_is_unreachable(self):
        """Another frequency in the same workspace is unreachable."""
        self.add_handle()
        self.add_handle("bob", scope="freq.other", pane_id="w1:p2")
        self.enter()
        with self.assertRaises(SystemExit):
            self.pm()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_explicit_sender_cannot_spoof_foreign_frequency_to_route_message(self):
        """A spoofed foreign sender cannot route a message."""
        self.add_handle()
        self.add_handle("foreign", scope="freq.other", pane_id="w2:p1")
        self.add_handle("bob", scope="freq.other", pane_id="w2:p2")
        self.enter()
        with self.assertRaises(SystemExit):
            self.pm(sender="freq.other:foreign")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_named_delivery_still_checks_physical_pane_workspace(self):
        """A named delivery still refuses a pane whose physical workspace moved."""
        self.add_handle("bob", pane_id="w2:p2", physical="w1")
        mid = self.seed_message("freq.team", "private", to="bob")
        radio.relay_tick(self.conn)
        self.assertEqual(self.pushed, [])
        self.assertEqual((self.delivery(mid)["status"], self.delivery(mid)["last_error"]),
                         ("pull", "workspace changed"))

    def test_show_foreign_id_neither_prints_body_nor_acknowledges(self):
        """Showing a foreign message prints nothing and does not acknowledge it."""
        self.add_handle()
        self.add_handle("alice", scope="freq.other", pane_id="w2:p1")
        mid = self.seed_message("freq.other", "foreign-private-body")
        self.enter()
        for by in (None, "freq.other:alice"):
            buf = io.StringIO()
            with self.subTest(by=by), contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit):
                    radio.cmd_show(self.conn, argparse.Namespace(message_id=mid, by=by))
            self.assertNotIn("foreign-private-body", buf.getvalue())
            self.assertEqual(self.delivery(mid)["status"], "pending")

    def test_show_own_message_can_acknowledge(self):
        """Showing an own message prints it and acknowledges the delivery."""
        self.add_handle()
        mid = self.seed_message("freq.team", "own-private-body")
        self.enter()
        out = self.capture(radio.cmd_show, argparse.Namespace(message_id=mid, by=None))
        self.assertIn("own-private-body", out)
        self.assertEqual(self.delivery(mid)["status"], "delivered")

    def test_log_and_inbox_do_not_expose_other_frequency(self):
        """log and inbox show only this frequency and refuse a foreign one."""
        self.add_handle()
        self.add_handle("alice", scope="freq.other", pane_id="w2:p1")
        own = self.seed_message("freq.team", "own-body-marker")
        foreign = self.seed_message("freq.other", "foreign-body-marker")
        self.enter()
        log = self.capture(radio.cmd_log, argparse.Namespace(limit=20))
        self.assertIn("own-body-marker", log)
        self.assertNotIn("foreign-body-marker", log)
        inbox = self.capture(radio.cmd_inbox, argparse.Namespace(handle=None, status="all", limit=20))
        self.assertIn("#" + str(own), inbox)
        self.assertNotIn("foreign-body-marker", inbox)
        with self.assertRaises(SystemExit):
            self.capture(radio.cmd_inbox, argparse.Namespace(
                handle="freq.other:alice", status="all", limit=20))
        self.assertEqual(self.delivery(foreign)["status"], "pending")

    def test_outside_herdr_cannot_read_named_frequency_messages(self):
        """Outside herdr a named frequency's messages are unreadable."""
        self.add_handle()
        mid = self.seed_message("freq.team", "named-private-marker")
        out = self.capture(radio.cmd_log, argparse.Namespace(limit=20))
        self.assertNotIn("named-private-marker", out)
        with self.assertRaises(SystemExit):
            self.capture(radio.cmd_show, argparse.Namespace(message_id=mid, by="freq.team:alice"))
        with self.assertRaises(SystemExit):
            self.capture(radio.cmd_inbox, argparse.Namespace(
                handle="freq.team:alice", status="all", limit=20))
        self.assertEqual(self.delivery(mid)["status"], "pending")


class FrequencyMigrationTest(FrequencyCase):
    """Schema 1 to 2: data preserved, physical workspace backfilled, pre-migration backup kept."""

    def test_v1_upgrade_preserves_data_backfills_physical_workspace_and_backs_up(self):
        """A real schema-1 ledger upgrades with a backup, a backfilled pane_workspace, and
        no second backup on reopen."""
        # Construct an actual schema-1 ledger, rather than mutating a schema-2
        # ledger's version marker and thereby skipping its real upgrade path.
        self.conn.close()
        self.root.joinpath("radio.db").unlink()
        legacy = sqlite3.connect(self.root / "radio.db")
        legacy.executescript("""
          PRAGMA user_version=1;
          CREATE TABLE handles (
            workspace TEXT NOT NULL DEFAULT '', name TEXT NOT NULL,
            session_ref TEXT NOT NULL DEFAULT 'manual', kind TEXT NOT NULL DEFAULT 'terminal',
            agent TEXT, agent_session TEXT, briefed_at TEXT, role TEXT, account TEXT,
            created_at TEXT NOT NULL, last_seen TEXT NOT NULL, PRIMARY KEY(workspace,name));
          CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL,
            from_ws TEXT NOT NULL DEFAULT '', from_handle TEXT NOT NULL,
            to_ws TEXT NOT NULL DEFAULT '', to_handle TEXT, text TEXT NOT NULL,
            ref TEXT, reply_required INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL,
            target_ws TEXT NOT NULL DEFAULT '', target TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT, created_at TEXT NOT NULL, delivered_at TEXT, last_attempt_at TEXT);
          CREATE TABLE accounts (name TEXT PRIMARY KEY, provider TEXT NOT NULL,
            home TEXT, env TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
          INSERT INTO accounts VALUES ('work','codex','C:/test/home','{}','t');
          INSERT INTO handles VALUES
            ('w1','alice','herdr:w1:p1','terminal','codex','saved-id',NULL,'review','work','t','t'),
            ('w2','alice','herdr:w2:p1','terminal',NULL,NULL,NULL,NULL,NULL,'t','t');
          INSERT INTO messages VALUES (7,'t','pm','w1','sender','w1','alice','preserved',NULL,1);
          INSERT INTO deliveries VALUES (9,7,'w1','alice','pending',0,NULL,'t',NULL,NULL);
        """)
        legacy.commit()
        legacy.close()
        before_files = set(self.root.iterdir())
        self.conn = radio.connect()
        self.addCleanup(self.conn.close)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 2)
        first = self.row("w1")
        self.assertEqual((first["pane_workspace"], first["agent_session"], first["role"], first["account"]),
                         ("w1", "saved-id", "review", "work"))
        self.assertEqual(self.row("w2")["pane_workspace"], "w2")
        self.assertEqual(self.conn.execute("SELECT text FROM messages WHERE id=7").fetchone()[0], "preserved")
        self.assertEqual(self.delivery(7)["id"], 9)
        backups = [p for p in set(self.root.iterdir()) - before_files if p.is_file()
                   and p.read_bytes().startswith(b"SQLite format 3\x00")]
        self.assertTrue(backups, "schema-1 ledger must be backed up before migration")
        with contextlib.closing(sqlite3.connect(backups[0].as_uri() + "?mode=ro", uri=True)) as snapshot:
            self.assertEqual(snapshot.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertNotIn("pane_workspace", [r[1] for r in snapshot.execute("PRAGMA table_info(handles)")])
        self.conn.close()
        backup_names = {p.name for p in backups}
        self.conn = radio.connect()
        self.addCleanup(self.conn.close)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM handles").fetchone()[0], 2)
        reopened_backups = {p.name for p in self.root.iterdir() if p.is_file()
                            and p.name != "radio.db"
                            and p.read_bytes().startswith(b"SQLite format 3\x00")}
        self.assertEqual(reopened_backups, backup_names,
                         "reopening schema 2 must not create another backup")


if __name__ == "__main__":
    unittest.main()
