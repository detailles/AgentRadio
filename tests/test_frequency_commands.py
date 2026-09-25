"""Launch and restore integration at the CLI boundary, without real processes."""

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

from test_frequencies import FrequencyCase, join_args, radio


class TerminalOutput(io.StringIO):
    """A stdout stub that claims to be a tty, so an interactive join proceeds."""

    def isatty(self):
        """Report a terminal so the join takes its interactive path."""
        return True


class FrequencyCommandsTest(FrequencyCase):
    """CLI boundaries: default handle changes, alias recovery, restore/view/part and the parser."""

    def test_default_handle_change_keeps_commands_usable_and_old_mail_for_pull(self):
        """Renaming the handle keeps the pane usable and leaves the old handle's mail for pull."""
        self.pane()
        self.enter()
        self.capture(radio.cmd_join, join_args(handle="alice"))
        old_message = self.seed_message("w1", "for the previous handle")
        self.capture(radio.cmd_join, join_args(handle="bob"))

        bindings = self.conn.execute(
            "SELECT workspace,name FROM handles WHERE session_ref='herdr:w1:p1'"
        ).fetchall()
        self.assertEqual([tuple(row) for row in bindings], [("w1", "bob")])
        self.assertEqual(self.row("w1", "alice")["session_ref"], "manual")
        self.assertEqual(self.delivery(old_message)["status"], "pull")
        self.assertEqual(radio.current_scope(self.conn), "w1")
        self.assertEqual(radio.resolve_identity(self.conn, None), ("w1", "bob"))
        self.capture(radio.cmd_join, join_args(handle="bob"))

        self.add_handle("peer", scope="w1", pane_id="w1:p2")
        self.pm(to="peer", text="new handle can send")
        message = self.conn.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((message["from_ws"], message["from_handle"], message["to_handle"]),
                         ("w1", "bob", "peer"))
        self.capture(radio.cmd_part, argparse.Namespace(handle="bob"))
        self.assertIsNone(self.row("w1", "bob"))
        self.assertEqual(radio.current_scope(self.conn), "w1")

    def test_existing_default_aliases_recover_from_the_live_pane_identity(self):
        """A stale alias is resolved from the live pane's label and session."""
        # Older releases retained the former handle after relabeling one pane.
        self.add_handle("alice", scope="w1", agent="codex", session="old-session")
        old_message = self.seed_message("w1", "old alias mail")
        self.add_handle("bob", scope="w1", agent="codex", session="current-session")
        self.panes["w1:p1"]["agent_session"] = {"value": "current-session"}
        self.enter()

        self.assertEqual(radio.resolve_identity(self.conn, None), ("w1", "bob"))
        self.capture(radio.cmd_join, join_args(handle="bob", provider="codex"))
        self.assertEqual(self.row("w1", "alice")["session_ref"], "manual")
        self.assertEqual(self.delivery(old_message)["status"], "pull")
        current = self.row("w1", "bob")
        self.assertEqual((current["session_ref"], current["agent"], current["agent_session"]),
                         ("herdr:w1:p1", "codex", "current-session"))
        self.assertEqual(radio.current_scope(self.conn), "w1")
        self.assertEqual(self.panes["w1:p1"]["label"], "bob")
        self.assertEqual(self.herdr_calls, [])

    def test_default_alias_recovery_requires_a_matching_live_pane(self):
        """Alias recovery needs a live pane whose label and workspace match."""
        self.add_handle("alice", scope="w1")
        self.add_handle("bob", scope="w1")
        self.enter()
        for live in (None, {"label": "unrelated", "workspace_id": "w1"},
                     {"label": "bob", "workspace_id": "w2"}):
            with self.subTest(live=live), patch.object(radio, "fetch_pane", return_value=live):
                with self.assertRaises(SystemExit):
                    radio.current_scope(self.conn)

    def test_default_alias_recovery_never_selects_among_named_bindings(self):
        """A named binding forbids alias guessing, even when the label matches."""
        self.add_handle("alice", scope="freq.team")
        self.add_handle("bob", scope="w1")
        self.enter()
        # The live label matches the default row, but a named binding forbids
        # guessing which frequency this conversation should belong to.
        with self.assertRaises(SystemExit):
            radio.current_scope(self.conn)
        with self.assertRaises(SystemExit):
            self.capture(radio.cmd_join, join_args(handle="bob"))
        self.assertEqual(self.row("freq.team", "alice")["session_ref"], "herdr:w1:p1")
        self.assertEqual(self.herdr_calls, [])

    def test_legacy_unscoped_membership_can_still_be_adopted_by_its_workspace(self):
        """A pre-scope row is adopted by its workspace without a disk search."""
        self.add_handle("alice", scope="", agent="codex", session="legacy-session")
        self.enter()
        self.capture(radio.cmd_join, join_args(provider="codex", resume=True))
        self.assertIsNone(self.row("", "alice"))
        current = self.row("w1", "alice")
        self.assertEqual((current["session_ref"], current["pane_workspace"], current["agent_session"]),
                         ("herdr:w1:p1", "w1", "legacy-session"))
        self.assertEqual(radio.current_scope(self.conn), "w1")
        self.session_search.assert_not_called()

    def test_parser_requires_one_frequency_selection(self):
        """--frequency and --workspace-frequency are mutually exclusive."""
        parser = radio.build_parser()
        args = parser.parse_args(["join", "alice", "--frequency", "team"])
        self.assertEqual(args.frequency, "team")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["join", "alice", "--frequency", "team", "--workspace-frequency"])

    def test_status_distinguishes_default_and_advanced(self):
        """radio frequency marks the default scope and named frequencies distinctly."""
        self.enter()
        output = self.capture(radio.cmd_frequency, argparse.Namespace())
        self.assertIn("(default)", output)
        self.add_handle()
        output = self.capture(radio.cmd_frequency, argparse.Namespace())
        self.assertIn("frequency: team (advanced)", output)

    def test_detached_named_agent_cannot_fall_back_to_workspace(self):
        """A detached named agent cannot silently fall back to the workspace."""
        self.add_handle()
        self.enter()
        os.environ["RADIO_JOINED_SCOPE"] = "freq.team"
        self.capture(radio.cmd_part, argparse.Namespace(handle="alice"))
        with self.assertRaisesRegex(SystemExit, "membership changed"):
            radio.current_scope(self.conn)

    def test_old_conversation_cannot_silently_follow_a_new_binding(self):
        """An old conversation scope cannot follow a new binding."""
        self.add_handle()
        self.enter()
        for previous in ("w1", "freq.other"):
            with self.subTest(previous=previous):
                os.environ["RADIO_JOINED_SCOPE"] = previous
                with self.assertRaisesRegex(SystemExit, "membership changed"):
                    radio.current_scope(self.conn)

    def test_view_uses_membership_and_clears_stale_environment(self):
        """The view inherits the pane's frequency and clears a stale override."""
        self.add_handle()
        self.enter()
        os.environ["RADIO_VIEW_FREQUENCY"] = "stale"
        with patch.object(radio, "exec_or_wait", return_value=0) as launch:
            radio.cmd_view(argparse.Namespace(frequency=None))
            self.assertEqual(launch.call_args.args[1]["RADIO_VIEW_FREQUENCY"], "team")
            self.enter("w1:p2")
            radio.cmd_view(argparse.Namespace(frequency=None))
            self.assertNotIn("RADIO_VIEW_FREQUENCY", launch.call_args.args[1])

    def test_restore_keeps_destination_frequency_and_recorded_session(self):
        """restore types the join command with the frequency and the recorded session."""
        self.add_handle(agent="codex", session="saved-session")
        self.panes["w1:p1"]["agent"] = None
        self.enter()
        with patch.object(radio, "session_present", return_value=True), patch.object(radio.time, "sleep"):
            self.capture(radio.cmd_restore, argparse.Namespace(handle="alice"))
        typed = next(call for call in self.herdr_calls if call[:2] == ("pane", "send-text"))
        self.assertEqual(typed[2], "w1:p1")
        self.assertEqual(typed[3], "radio join alice --provider codex --resume --frequency team")

    def test_restore_recovery_instructions_keep_frequency(self):
        """Restore recovery instructions keep the frequency."""
        self.add_handle("operator", pane_id="w1:p2")
        self.enter("w1:p2")
        self.add_handle(agent="codex", session="saved-session")
        self.panes.pop("w1:p1")
        with self.assertRaisesRegex(SystemExit, "radio join alice --provider codex --frequency team"):
            radio.cmd_restore(self.conn, argparse.Namespace(handle="alice"))
        self.conn.execute("UPDATE handles SET session_ref='manual' WHERE name='alice'")
        self.conn.commit()
        with self.assertRaisesRegex(SystemExit, "radio join alice --provider codex --frequency team"):
            radio.cmd_restore(self.conn, argparse.Namespace(handle="alice"))

    def test_opencode_same_handle_keeps_separate_frequency_briefings(self):
        """Two frequencies of one handle get separate opencode configs and briefings."""
        configs = []
        for number, frequency in enumerate(("one", "two"), start=1):
            pane_id = f"w1:p{number}"
            self.pane(pane_id)
            self.enter(pane_id)
            output = TerminalOutput()
            with contextlib.redirect_stdout(output), patch.object(radio.sys.stdin, "isatty", return_value=True), \
                    patch.object(radio, "launch_agent", return_value=0) as launch:
                radio.cmd_join(self.conn, join_args(pane=pane_id, provider="opencode", new=True,
                                                   no_launch=False, frequency=frequency))
            config_path = Path(launch.call_args.args[1]["OPENCODE_CONFIG"])
            self.assertEqual(launch.call_args.args[1]["RADIO_JOINED_SCOPE"], f"freq.{frequency}")
            self.assertTrue(config_path.is_relative_to(self.root))
            configs.append(config_path)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            briefing = Path(config["instructions"][0]).read_text(encoding="utf-8")
            self.assertIn(f'on frequency "{frequency}"', briefing)
        self.assertNotEqual(configs[0], configs[1])
        self.session_search.assert_not_called()
