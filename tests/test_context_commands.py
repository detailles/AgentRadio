"""Read-only context CLI boundaries; no real Herdr, credentials, or provider I/O."""

import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from test_radio import RadioTestCase, radio


SESSION = "019fcb00-0000-7000-8000-000000000001"


class ContextCommandsTest(RadioTestCase):
    """Scope and live identity checks precede any provider-session inspection."""

    def setUp(self):
        super().setUp()
        self.panes = {}
        self.home = self.tmp / "codex-default"
        self.probe = self.tmp / "session.jsonl"
        self.probe.write_text("", encoding="utf-8")
        self.measurement = {
            "model": "gpt-6-luna", "effort": "xhigh", "context_tokens": 2500,
            "context_window": 10000,
            "observed_at": datetime.now(timezone.utc).isoformat(), "error": None,
        }
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"CODEX_HOME": str(self.home)}))
        self.scope = self.stack.enter_context(patch.object(radio, "current_scope", return_value=""))
        self.stack.enter_context(patch.object(radio, "fetch_pane", side_effect=self.panes.get))
        self.stack.enter_context(patch.object(radio, "workspace_labels", return_value={}))
        self.find = self.stack.enter_context(
            patch.object(radio, "context_session_file", return_value=self.probe))
        self.read = self.stack.enter_context(
            patch.object(radio, "read_codex_context", return_value=self.measurement))
        # Any accidental fall-through to provider or unmocked Herdr access fails closed.
        self.stack.enter_context(patch.object(radio, "herdr", side_effect=AssertionError("real Herdr call")))
        self.stack.enter_context(patch.object(radio, "read_provider_usage",
                                             side_effect=AssertionError("provider quota call")))

    def bind(self, name="alice", workspace="w1", pane="w1:p1", provider="codex",
             session=SESSION, account=None, physical=None):
        """Create a ledger row and corresponding live pane with independently editable identity."""
        self.add_handle(name, ref=f"herdr:{pane}", agent=provider,
                        agent_session=session, workspace=workspace, account=account,
                        role="Local custom role; must not enter generic context output")
        location = physical or workspace
        self.conn.execute("UPDATE handles SET pane_workspace=? WHERE workspace=? AND name=?",
                          (location, workspace, name))
        self.conn.commit()
        self.panes[pane] = {
            "id": pane, "workspace_id": location, "label": radio.pane_label(name, workspace),
            "agent": provider, "agent_session": {"value": session},
            "agent_status": "idle",
        }

    def snapshot(self, *flags):
        """Use the public parser for all argument defaults."""
        args = radio.build_parser().parse_args(["tools", "context", *flags])
        return radio.context_snapshot(self.conn, args)

    def add_account(self, name, provider="codex", home=None, env=None):
        self.conn.execute(
            "INSERT INTO accounts(name,provider,home,env,created_at) VALUES (?,?,?,?,?)",
            (name, provider, str(home) if home else None, json.dumps(env or {}), radio.now()))
        self.conn.commit()

    def assert_no_context(self, agent, error):
        self.assertEqual(agent["error"], error)
        for key in ("model", "effort", "context_tokens", "context_window", "context_percent"):
            self.assertIsNone(agent[key], key)
        self.read.assert_not_called()

    def test_valid_live_binding_yields_approximate_last_response_context(self):
        self.bind()
        result = self.snapshot()
        self.assertTrue(result["approximate"])
        agent = result["agents"][0]
        self.assertEqual((agent["handle"], agent["workspace"], agent["session"]),
                         ("alice", "w1", SESSION))
        self.assertEqual(agent["context_percent"], 25)
        self.assertEqual(agent["context_tokens"], 2500)
        self.assertFalse(agent["stale"])
        self.assertIsNone(agent["error"])
        self.find.assert_called_once_with(self.home, SESSION)
        self.read.assert_called_once_with(self.probe, SESSION)

    def test_inside_workspace_only_its_handles_are_visible(self):
        self.bind("alice")
        self.bind("bob", workspace="w2", pane="w2:p1")
        self.scope.return_value = "w1"
        self.assertEqual([a["handle"] for a in self.snapshot()["agents"]], ["alice"])
        self.assertEqual(self.read.call_count, 1)

    def test_outside_pane_named_frequency_members_are_private(self):
        self.bind("alice")
        self.bind("secret", workspace="freq.secret", pane="w2:p1", physical="w2")
        self.assertEqual([a["handle"] for a in self.snapshot()["agents"]], ["alice"])
        self.assertEqual(self.read.call_count, 1)

    def test_inside_named_frequency_sees_only_that_frequency(self):
        self.bind("alice")
        self.bind("secret", workspace="freq.secret", pane="w2:p1", physical="w2")
        self.scope.return_value = "freq.secret"
        agents = self.snapshot()["agents"]
        self.assertEqual([a["handle"] for a in agents], ["secret"])
        self.assertEqual(agents[0]["context_percent"], 25)

    def test_explicit_workspace_label_resolves_and_filters(self):
        self.bind("alice")
        self.bind("bob", workspace="w2", pane="w2:p1")
        with patch.object(radio, "resolve_workspace_spec", return_value="w2") as resolve:
            self.assertEqual([a["handle"] for a in self.snapshot("--workspace", "Other")["agents"]],
                             ["bob"])
        resolve.assert_called_once_with("Other", self.conn)

    def test_explicit_workspace_cannot_escape_named_frequency(self):
        self.scope.return_value = "freq.secret"
        with patch.object(radio, "resolve_workspace_spec", return_value="w1"):
            with self.assertRaises(SystemExit):
                self.snapshot("--workspace", "w1")
        self.read.assert_not_called()

    def test_reused_pane_session_cannot_inherit_previous_context(self):
        self.bind()
        self.panes["w1:p1"]["agent_session"] = {"value": "different-session"}
        self.assert_no_context(self.snapshot()["agents"][0], "pane_mismatch")

    def test_reused_pane_alias_cannot_inherit_previous_context(self):
        self.bind()
        self.panes["w1:p1"]["label"] = "someone-else"
        self.assert_no_context(self.snapshot()["agents"][0], "pane_mismatch")

    def test_physical_workspace_and_provider_must_match(self):
        self.bind()
        for key, value in (("workspace_id", "w9"), ("agent", "claude")):
            with self.subTest(key=key):
                pane = dict(self.panes["w1:p1"])
                pane[key] = value
                with patch.object(radio, "fetch_pane", return_value=pane):
                    self.assert_no_context(self.snapshot()["agents"][0], "pane_mismatch")

    def test_missing_pane_does_not_read_saved_session(self):
        self.bind()
        self.panes.clear()
        self.assert_no_context(self.snapshot()["agents"][0], "pane_unavailable")

    def test_unsupported_provider_is_unknown_not_zero(self):
        self.bind(provider="claude")
        self.assert_no_context(self.snapshot()["agents"][0], "unsupported_provider")
        self.find.assert_not_called()

    def test_missing_session_is_unknown_not_zero(self):
        self.bind(session=None)
        self.assert_no_context(self.snapshot()["agents"][0], "session_missing")

    def test_missing_rollout_is_unknown_not_zero(self):
        self.bind()
        self.find.return_value = None
        self.assert_no_context(self.snapshot()["agents"][0], "session_unavailable")

    def test_named_account_env_home_takes_precedence_and_is_not_disclosed(self):
        home = self.tmp / "named-home"
        override = self.tmp / "override-home"
        self.add_account("work", home=home,
                         env={"CODEX_HOME": str(override), "PRIVATE_TOKEN": "secret-fixture"})
        self.bind(account="work")
        result = self.snapshot()
        self.find.assert_called_once_with(override, SESSION)
        self.assertEqual(result["agents"][0]["account"], "work")
        text = json.dumps(result)
        self.assertNotIn("secret-fixture", text)
        self.assertNotIn(str(override), text)
        self.assertNotIn(str(home), text)

    def test_named_account_home_works_without_env_override(self):
        home = self.tmp / "named-home"
        self.add_account("work", home=home)
        self.bind(account="work")
        self.snapshot()
        self.find.assert_called_once_with(home, SESSION)

    def test_missing_or_wrong_provider_account_never_falls_back(self):
        self.bind(account="work")
        self.assert_no_context(self.snapshot()["agents"][0], "account_unavailable")
        self.add_account("work", provider="claude", home=self.tmp / "claude-home")
        self.assert_no_context(self.snapshot()["agents"][0], "account_unavailable")
        self.find.assert_not_called()

    def test_old_sample_is_marked_stale(self):
        self.bind()
        self.measurement["observed_at"] = "2020-01-01T00:00:00+00:00"
        agent = self.snapshot()["agents"][0]
        self.assertTrue(agent["stale"])
        self.assertGreater(agent["age_seconds"], 300)

    def test_pane_replaced_during_read_discards_collected_context(self):
        self.bind()
        previous = self.panes["w1:p1"]
        replacement = dict(previous, agent_session={"value": "new-session"})
        with patch.object(radio, "fetch_pane", side_effect=[previous, replacement]):
            agent = self.snapshot()["agents"][0]
        self.assertEqual(agent["error"], "pane_mismatch")
        for key in ("model", "context_tokens", "context_window", "context_percent", "observed_at"):
            self.assertIsNone(agent[key], key)
        self.read.assert_called_once()

    def test_missing_capacity_does_not_invent_context_percent(self):
        self.bind()
        self.measurement["context_window"] = None
        self.measurement["observed_at"] = "2000-01-01T00:00:00Z"
        agent = self.snapshot()["agents"][0]
        self.assertEqual(agent["context_tokens"], 2500)
        self.assertIsNone(agent["context_window"])
        self.assertIsNone(agent["context_percent"])
        self.assertGreater(agent["age_seconds"], 300)
        self.assertTrue(agent["stale"])

    def test_json_command_omits_handle_role(self):
        self.bind()
        output = io.StringIO()
        args = radio.build_parser().parse_args(["tools", "context", "--json"])
        with contextlib.redirect_stdout(output):
            self.assertEqual(radio.cmd_tools_context(self.conn, args), 0)
        result = json.loads(output.getvalue())
        self.assertNotIn("role", result["agents"][0])
        self.assertNotIn("Local custom role", output.getvalue())

    def test_default_and_explicit_table_are_one_shot(self):
        self.bind()
        for flags in ([], ["--table"]):
            with self.subTest(flags=flags):
                args = radio.build_parser().parse_args(["tools", "context", *flags])
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(radio.cmd_tools_context(self.conn, args), 0)
                self.assertIn("alice", output.getvalue())
                self.assertIn("25", output.getvalue())


class ContextReadonlyTest(RadioTestCase):
    """The context command must not initialize or migrate the Radio ledger."""

    def test_missing_ledger_does_not_create_directory_or_database(self):
        missing = self.tmp / "not-created" / "radio.db"
        with patch.object(radio, "DB_PATH", missing), patch.object(radio, "STATE_DIR", missing.parent):
            with contextlib.closing(radio.context_connect()) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM handles").fetchone()[0], 0)
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("CREATE TABLE cannot_write(id INTEGER)")
        self.assertFalse(missing.parent.exists())

    def test_existing_ledger_connection_rejects_writes(self):
        with contextlib.closing(radio.context_connect()) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], radio.SCHEMA_VERSION)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM handles")

    def test_old_schema_is_rejected_without_migration(self):
        old = self.tmp / "old.db"
        with contextlib.closing(sqlite3.connect(old)) as conn:
            conn.execute("CREATE TABLE marker(id INTEGER)")
            conn.execute("PRAGMA user_version=1")
        before = old.read_bytes()
        with patch.object(radio, "DB_PATH", old):
            with self.assertRaises(SystemExit):
                radio.context_connect()
        self.assertEqual(old.read_bytes(), before)

    def test_main_context_dispatch_bypasses_writable_connect(self):
        with patch.object(radio, "connect", side_effect=AssertionError("writable connect")), \
             patch.object(radio, "current_scope", return_value=""), \
             patch.object(radio, "workspace_labels", return_value={}), \
             patch("sys.argv", ["radio", "tools", "context", "--json"]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(radio.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["agents"], [])


if __name__ == "__main__":
    unittest.main()
