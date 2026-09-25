"""Dashboard frequency isolation with an in-memory ledger and mocked Herdr.

No real processes, user state or live database are accessed.
"""

import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sqlite3
import subprocess
import unittest
from unittest.mock import Mock, call, patch


REPO = Path(__file__).resolve().parent.parent
loader = importlib.machinery.SourceFileLoader(
    "frequency_radio_view", str(REPO / "bin" / "radio-view")
)
spec = importlib.util.spec_from_file_location(loader.name, loader.path, loader=loader)
view = importlib.util.module_from_spec(spec)
try:
    loader.exec_module(view)
    HAS_VIEW = True
except SystemExit:
    HAS_VIEW = False


@unittest.skipUnless(HAS_VIEW, "view deps (textual) not installed")
class FrequencyViewTest(unittest.TestCase):
    """Dashboard queries and labels stay inside one frequency; operator mode stays explicit."""

    def setUp(self):
        """Build an in-memory ledger with two workspaces, two frequencies and mixed counts."""
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE handles (
                workspace TEXT, name TEXT, session_ref TEXT,
                pane_workspace TEXT, agent TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, from_ws TEXT, to_ws TEXT,
                from_handle TEXT, to_handle TEXT, ts TEXT, text TEXT
            );
            CREATE TABLE deliveries (target_ws TEXT, status TEXT);
        """)
        self.conn.executemany("INSERT INTO handles VALUES (?, ?, ?, ?, ?)", [
            ("w1", "planner", "herdr:w1:p1", "", "codex"),
            ("w2", "planner", "herdr:w2:p1", "", "codex"),
            ("freq.alpha", "planner", "herdr:w1:p2", "w1", "codex"),
            ("freq.alpha", "reviewer", "herdr:w2:p2", "w2", "codex"),
            ("freq.beta", "planner", "herdr:w1:p3", "w1", "codex"),
        ])
        self.conn.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)", [
            (mid, scope, scope, "planner", "reviewer", "2026-09-25T09:00:00+00:00", scope)
            for mid, scope in enumerate(
                ("w1", "freq.alpha", "freq.beta", "freq.alpha", "w2", "freq.beta"), 1
            )
        ])
        self.conn.executemany("INSERT INTO deliveries VALUES (?, ?)", [
            ("w1", "pending"), ("w2", "failed"),
            ("freq.alpha", "pending"), ("freq.alpha", "delivered"),
            ("freq.beta", "failed"), ("freq.beta", "failed"),
        ])

    def rows(self, query):
        """Run a query tuple and return its rows."""
        return self.conn.execute(*query).fetchall()

    def make_app(self, frequency="alpha"):
        """Build a RadioView scoped to the given frequency inside herdr."""
        with patch.dict(os.environ, {
            "HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1",
            "RADIO_VIEW_FREQUENCY": frequency,
        }):
            return view.RadioView()

    def test_explicit_frequency_overrides_physical_workspace(self):
        """An explicit frequency overrides the physical workspace; an empty one falls back to it."""
        app = self.make_app()
        self.assertEqual(app.scope, "freq.alpha")
        self.assertFalse(app.show_all)
        self.assertEqual(self.make_app("").scope, "w1")

    def test_named_handles_include_multiple_workspaces_without_other_frequencies(self):
        """A frequency's roster spans its workspaces and excludes other frequencies."""
        for show_all in (False, True):
            rows = self.rows(view.handles_query("freq.alpha", show_all))
            self.assertEqual([row["name"] for row in rows], ["planner", "reviewer"])
            self.assertEqual({row["pane_workspace"] for row in rows}, {"w1", "w2"})

    def test_live_and_history_queries_stay_on_the_same_frequency(self):
        """Live and history queries stay on the same frequency in both modes."""
        for show_all in (False, True):
            live = self.rows(view.messages_query("freq.alpha", show_all, 1))
            history = self.rows(view.history_query("freq.alpha", show_all, 50))
            self.assertEqual([row["id"] for row in live], [2, 4])
            self.assertEqual([row["id"] for row in history], [4, 2])
        self.assertEqual(
            [row["id"] for row in self.rows(view.history_query("freq.alpha", False, 1))], [4]
        )

    def test_counts_exclude_other_frequencies_even_when_show_all_is_set(self):
        """Counts never include another frequency, even in overview mode."""
        for show_all in (False, True):
            counts = {row["status"]: row["c"] for row in self.rows(
                view.counts_query("freq.alpha", show_all)
            )}
            self.assertEqual(counts, {"pending": 1, "delivered": 1})

    def test_default_workspace_counts_and_operator_overview(self):
        """Default scope counts stay workspace-only; the overview covers everything."""
        rows = self.rows(view.counts_query("w1", False))
        self.assertEqual([(row["status"], row["c"]) for row in rows], [("pending", 1)])
        self.assertEqual(len(self.rows(view.handles_query("w1", False))), 1)
        self.assertEqual(len(self.rows(view.handles_query("w1", True))), 5)
        self.assertEqual(len(self.rows(view.messages_query("w1", True, 0))), 6)
        self.assertEqual(sum(row["c"] for row in self.rows(view.counts_query("w1", True))), 6)

    def test_named_view_all_toggle_is_hidden_and_does_not_rescope(self):
        """A named view hides the all toggle and cannot be rescoped."""
        app = self.make_app()
        with patch.object(app, "reload_stream") as reload_stream, \
                patch.object(app, "refresh_handles") as refresh_handles:
            app.action_toggle_all()
        self.assertFalse(app.show_all)
        reload_stream.assert_not_called()
        refresh_handles.assert_not_called()
        self.assertFalse(app.check_action("toggle_all", ()))

    def test_default_view_can_still_open_operator_overview(self):
        """A default view can still open the operator overview."""
        app = self.make_app("")
        with patch.object(app, "reload_stream"), patch.object(app, "refresh_handles"):
            app.action_toggle_all()
        self.assertTrue(app.show_all)
        self.assertTrue(app.check_action("toggle_all", ()))

    def test_named_handle_requires_qualified_pane_label(self):
        """A named handle's dot needs the name@frequency pane label."""
        handle = self.rows(view.handles_query("freq.alpha", False))[0]
        pane = {"label": "planner@alpha", "workspace_id": "w1", "agent_status": "idle"}
        dot, note = view.dot_for(handle, {"w1:p2": pane})
        self.assertEqual((dot.plain, note), ("●", "idle"))
        for wrong_label in ("planner", "planner@beta"):
            dot, note = view.dot_for(handle, {"w1:p2": {**pane, "label": wrong_label}})
            self.assertEqual(note, "reused")
        self.assertEqual(view.handle_label(self.rows(view.handles_query("w1", False))[0]), "planner")

    def test_compact_roster_disambiguates_named_handles(self):
        """The compact roster disambiguates same-name handles by frequency."""
        handles = self.rows(view.handles_query("w1", True))
        states = {
            row["session_ref"].removeprefix("herdr:"): {
                "label": view.handle_label(row), "agent_status": "idle"
            } for row in handles
        }
        roster = view.compact_roster(handles, states).plain
        self.assertIn("●planner@alpha", roster)
        self.assertIn("●planner@beta", roster)

    def test_pane_discovery_never_sends_frequency_as_herdr_workspace(self):
        """Pane discovery sends only real workspace ids, never a frequency."""
        with patch.object(view, "_pane_list", return_value={}) as pane_list:
            view.pane_states({"w1": "Main", "freq.alpha": "frequency alpha", "w2": "Other"})
        self.assertEqual(pane_list.call_args_list, [call("--workspace", "w1"), call("--workspace", "w2")])

    def test_operator_message_scope_is_explicit(self):
        """Overview messages carry an explicit scope label; scoped ones do not."""
        message = self.conn.execute("SELECT * FROM messages WHERE id=2").fetchone()
        self.assertIn("[named frequency alpha]", view.fmt_message(message, show_scope=True).plain)
        self.assertNotIn("[named frequency alpha]", view.fmt_message(message).plain)
        self.assertEqual(view.scope_label("w1", {"w1": "Main"}), "Main")

    def test_header_displays_only_named_frequency_counts(self):
        """The header names the frequency and shows only its counts."""
        app = self.make_app()
        widgets = {"#bar-left": Mock(), "#bar-right": Mock()}
        with patch.object(view, "connect", return_value=self.conn), \
                patch.object(view, "relay_alive", return_value=True), \
                patch.object(app, "query_one", side_effect=lambda name, *_: widgets[name]):
            app.refresh_header()
        self.assertEqual(widgets["#bar-left"].update.call_args.args[0], "◈ Radio · named frequency alpha")
        stats = widgets["#bar-right"].update.call_args.args[0].plain
        self.assertIn("1 pending · 0 failed · 1 delivered", stats)

    def test_stream_reloads_and_tails_only_the_selected_frequency(self):
        """The stream reloads and tails only the selected frequency, even with show_all set."""
        app = self.make_app()
        app.show_all = True  # Query guards must also protect reload/poll callers.
        stream = Mock()
        with patch.object(view, "connect", return_value=self.conn), \
                patch.object(app, "query_one", return_value=stream):
            app.reload_stream()
            self.assertEqual(app.last_message_id, 4)
            self.conn.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)", [
                (7, "freq.alpha", "freq.alpha", "planner", "reviewer", "2026-09-25T09:00:00+00:00", "alpha followup"),
                (8, "freq.beta", "freq.beta", "planner", "reviewer", "2026-09-25T09:00:00+00:00", "beta followup"),
            ])
            app.poll_messages()
        self.assertEqual(app.last_message_id, 7)
        rendered = [item.args[0].plain for item in stream.write.call_args_list]
        self.assertEqual(len(rendered), 3)
        self.assertTrue(all("alpha" in text and "beta" not in text for text in rendered))

    def test_operator_roster_groups_frequencies_with_explicit_labels(self):
        """The operator roster groups named frequencies with explicit labels."""
        app = self.make_app("")
        app.show_all = True
        roster = Mock()
        labels = {"w1": "Main", "w2": "Other"}
        with patch.object(view, "connect", return_value=self.conn), \
                patch.object(view, "workspace_labels", return_value=labels), \
                patch.object(view, "pane_states", return_value={}) as pane_states, \
                patch.object(app, "query_one", return_value=roster), \
                patch.object(app, "refresh_header"):
            app.refresh_handles()
        pane_states.assert_called_once_with(labels)
        rendered = roster.update.call_args.args[0].plain
        self.assertIn("named frequency alpha\n", rendered)
        self.assertIn("named frequency beta\n", rendered)
        self.assertIn("Main\n", rendered)
        self.assertIn("Other\n", rendered)

    def test_named_dashboard_adopts_qualified_label(self):
        """A named dashboard renames its pane to Radio@frequency."""
        result = subprocess.CompletedProcess([], 0, '{"result":{"pane":{"label":"Radio"}}}')
        with patch.dict(os.environ, {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p9", "RADIO_VIEW_FREQUENCY": "alpha"}), \
                patch.object(view, "herdr", return_value=result) as herdr:
            view.adopt_pane()
        herdr.assert_any_call("pane", "rename", "w1:p9", "Radio@alpha")

    def test_default_frequency_header_shows_number_workspace_and_project(self):
        """The default header shows the workspace number, id and project label."""
        self.assertEqual(
            view.header_scope_label("w4", False, {"w4": "Backend"}),
            "Frequency 004 · workspace w4 · Backend",
        )
        self.assertEqual(view.frequency_number("w1234"), "1234")
        self.assertEqual(view.frequency_number("opaque-workspace"), "opaque-workspace")
        self.assertEqual(view.frequency_number("w4-extra"), "w4-extra")

    def test_custom_numeric_frequency_is_distinct_and_overview_is_explicit(self):
        """A numeric frequency name stays distinct from the workspace number; overview says
        all frequencies."""
        self.assertEqual(view.header_scope_label("freq.004", False, {}), "named frequency 004")
        self.assertEqual(view.header_scope_label("freq.004", True, {}), "named frequency 004")
        self.assertEqual(view.header_scope_label("w4", True, {}), "all frequencies")

    def test_default_dashboard_adopts_numbered_label(self):
        """The default dashboard renames its pane to Radio NNN."""
        result = subprocess.CompletedProcess([], 0, '{"result":{"pane":{"label":"Radio"}}}')
        with patch.dict(os.environ, {
            "HERDR_ENV": "1", "HERDR_PANE_ID": "w4:p9",
            "HERDR_WORKSPACE_ID": "w4", "RADIO_VIEW_FREQUENCY": "",
        }), patch.object(view, "herdr", return_value=result) as herdr:
            view.adopt_pane()
        herdr.assert_any_call("pane", "rename", "w4:p9", "Radio 004")

if __name__ == "__main__":
    unittest.main()
