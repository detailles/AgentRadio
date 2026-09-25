#!/usr/bin/env python3
"""Relay race and frequency-boundary regressions on temporary SQLite state.

Herdr lookup and delivery are mocked by FrequencyCase. Competing writes use a
second connection to that test's temporary ledger; no threads or processes run.
"""

from datetime import datetime, timedelta, timezone
import sqlite3
import unittest
from unittest.mock import patch

try:
    from .test_frequencies import FrequencyCase, join_args, radio
except ImportError:  # unittest discover -s tests imports modules without a package.
    from test_frequencies import FrequencyCase, join_args, radio


class FrequencyRelayRaceTest(FrequencyCase):
    def competing_connection(self):
        connection = sqlite3.connect(self.root / "radio.db", timeout=0)
        self.addCleanup(connection.close)
        return connection

    def test_binding_retuned_before_final_lock_blocks_push_despite_stale_pane(self):
        self.add_handle(scope="freq.old")
        mid = self.seed_message("freq.old", "must stay on the old frequency")
        self.conn.execute(
            "UPDATE deliveries SET focus_hold_since=? WHERE message_id=?", (radio.now(), mid)
        )
        self.conn.commit()
        stale_pane = dict(self.panes["w1:p1"])
        competing = self.competing_connection()
        retuned = []

        def lookup_with_concurrent_retune(pane_id):
            self.assertEqual(pane_id, "w1:p1")
            if not retuned:
                self.assertFalse(self.conn.in_transaction)
                # Preserve the old recipient for pull and bind the same pane
                # elsewhere. Leave its delivery pending to exercise the relay's
                # own final guard, independent of join's backlog demotion.
                with competing:
                    competing.execute(
                        "UPDATE handles SET session_ref='manual' "
                        "WHERE workspace='freq.old' AND name='alice'"
                    )
                    competing.execute(
                        """INSERT INTO handles(workspace,name,session_ref,pane_workspace,
                           created_at,last_seen) VALUES (?,?,?,?,?,?)""",
                        ("freq.new", "alice", "herdr:w1:p1", "w1", radio.now(), radio.now()),
                    )
                retuned.append(True)
            # Herdr may still report an old label even after the ledger changed.
            return dict(stale_pane)

        with patch.object(radio, "fetch_pane", side_effect=lookup_with_concurrent_retune):
            radio.relay_tick(self.conn)

        self.assertTrue(retuned, "the competing commit must happen during the relay tick")
        self.assertEqual(self.row("freq.old")["session_ref"], "manual")
        self.assertEqual(self.row("freq.new")["session_ref"], "herdr:w1:p1")
        self.assertEqual(self.pushed, [])
        self.assertEqual(self.delivery(mid)["status"], "pull")
        self.assertIn("binding", self.delivery(mid)["last_error"])
        self.assertIsNone(self.delivery(mid)["focus_hold_since"])
        self.assertFalse(self.conn.in_transaction)

    def test_push_holds_write_lock_and_releases_it_before_later_retune(self):
        self.add_handle()
        mid = self.seed_message("freq.team", "deliver while binding is locked")
        competing = self.competing_connection()
        attempted = []

        def push_while_retune_attempts(pane_id, text):
            self.assertTrue(self.conn.in_transaction)
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                competing.execute(
                    "UPDATE handles SET session_ref='manual' "
                    "WHERE workspace='freq.team' AND name='alice'"
                )
            competing.rollback()
            attempted.append(True)
            return self.fake_push(pane_id, text)

        with patch.object(radio, "push_to_pane", side_effect=push_while_retune_attempts):
            radio.relay_tick(self.conn)

        self.assertEqual(attempted, [True])
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(self.delivery(mid)["status"], "delivered")
        self.assertFalse(self.conn.in_transaction)
        # The very same competing update must work once delivery has finished.
        with competing:
            competing.execute(
                "UPDATE handles SET session_ref='manual' "
                "WHERE workspace='freq.team' AND name='alice'"
            )
        self.assertEqual(self.row()["session_ref"], "manual")

    def test_duplicate_active_bindings_block_named_delivery_even_with_matching_label(self):
        self.add_handle(scope="freq.team")
        self.add_handle(scope="freq.other")
        self.panes["w1:p1"]["label"] = "alice@team"
        mid = self.seed_message("freq.team", "ambiguous pane must get no push")

        radio.relay_tick(self.conn)

        self.assertEqual(self.pushed, [])
        self.assertEqual(self.delivery(mid)["status"], "pull")
        self.assertIn("binding", self.delivery(mid)["last_error"])


class FrequencyRelayFocusTest(FrequencyCase):
    """Named delivery keeps upstream composer deferrals inside the retune lock."""

    def setUp(self):
        """Bind a focused Codex pane and keep composer reads fully mocked."""
        super().setUp()
        self.add_handle(agent="codex", session="named-session")
        self.panes["w1:p1"]["focused"] = True
        self.mid = self.seed_message("freq.team", "wait for the named recipient")
        self.draft = self.stack.enter_context(
            patch.object(radio, "composer_has_draft", return_value=False)
        )

    def backdate_hold(self):
        """Expire the hold without sleeping or changing any process clock."""
        since = (datetime.now(timezone.utc) - timedelta(seconds=radio.FOCUS_HOLD_S + 5)).isoformat()
        self.conn.execute(
            "UPDATE deliveries SET focus_hold_since=? WHERE message_id=?", (since, self.mid)
        )
        self.conn.commit()
        return since

    def assert_deferred(self):
        """Waiting is pending with no consumed attempt or submission timestamp."""
        row = self.delivery(self.mid)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)
        self.assertIsNone(row["last_attempt_at"])
        self.assertEqual(self.pushed, [])
        self.assertFalse(self.conn.in_transaction)

    def test_named_focus_holds_then_draft_waits_and_empty_composer_delivers_once(self):
        """The initial wait, expired draft hold and eventual delivery retain named scope."""
        radio.relay_tick(self.conn)
        self.assert_deferred()
        self.assertIsNotNone(self.delivery(self.mid)["focus_hold_since"])
        self.draft.assert_not_called()

        since = self.backdate_hold()
        self.draft.return_value = True
        events = radio.relay_tick(self.conn)
        self.assert_deferred()
        self.assertEqual(self.delivery(self.mid)["focus_hold_since"], since)
        self.draft.assert_called_once_with("codex", "w1:p1")
        self.assertTrue(any("user is typing" in event for event in events))

        self.draft.return_value = False
        radio.relay_tick(self.conn)
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(self.pushed[0][0], "w1:p1")
        self.assertIn("frequency=team", self.pushed[0][1])
        self.assertEqual(self.delivery(self.mid)["status"], "delivered")
        self.assertIsNone(self.delivery(self.mid)["focus_hold_since"])
        radio.relay_tick(self.conn)
        self.assertEqual(len(self.pushed), 1)

    def test_focus_gained_between_snapshot_and_locked_recheck_blocks_push(self):
        """A newly focused live pane must override the earlier unfocused snapshot."""
        focused = dict(self.panes["w1:p1"])
        unfocused = dict(focused, focused=False)
        with patch.object(radio, "fetch_pane", side_effect=[unfocused, focused]) as fetch:
            radio.relay_tick(self.conn)
        self.assertEqual(fetch.call_count, 2)
        self.assert_deferred()
        self.assertIsNotNone(self.delivery(self.mid)["focus_hold_since"])
        self.draft.assert_not_called()

    def test_locked_recheck_uses_updated_hold_clock_not_the_batch_snapshot(self):
        """A competing fresh hold cannot be bypassed using the batch's expired timer."""
        self.backdate_hold()
        competing = sqlite3.connect(self.root / "radio.db", timeout=0)
        self.addCleanup(competing.close)
        refreshed = []

        def refresh_before_lock(pane_id):
            """Commit a new hold after batch selection but before BEGIN IMMEDIATE."""
            if not refreshed:
                self.assertFalse(self.conn.in_transaction)
                since = radio.now()
                with competing:
                    competing.execute(
                        "UPDATE deliveries SET focus_hold_since=? WHERE message_id=?", (since, self.mid)
                    )
                refreshed.append(since)
            return dict(self.panes[pane_id])

        with patch.object(radio, "fetch_pane", side_effect=refresh_before_lock):
            radio.relay_tick(self.conn)
        self.assert_deferred()
        self.assertEqual(self.delivery(self.mid)["focus_hold_since"], refreshed[0])
        self.draft.assert_not_called()

    def test_retuning_a_held_delivery_clears_hold_and_never_repromotes_it(self):
        """Once the agent exits, retuning leaves old traffic for pull without a focus timer."""
        radio.relay_tick(self.conn)
        self.assertIsNotNone(self.delivery(self.mid)["focus_hold_since"])
        self.panes["w1:p1"]["agent"] = None
        self.enter()
        self.capture(radio.cmd_join, join_args(frequency="other"))
        row = self.delivery(self.mid)
        self.assertEqual((row["status"], row["last_error"]), ("pull", "frequency changed"))
        self.assertIsNone(row["focus_hold_since"])
        self.assertEqual(row["attempts"], 0)
        self.assertIsNone(row["last_attempt_at"])
        radio.relay_tick(self.conn)
        self.assertEqual(self.pushed, [])
        self.assertEqual(self.delivery(self.mid)["status"], "pull")

    def test_unconfirmed_named_push_clears_hold_and_is_never_blindly_retried(self):
        """An expired hold does not turn an ambiguous submission into an automatic retry."""
        self.backdate_hold()
        with patch.object(radio, "push_to_pane", return_value=(False, "delivery_unconfirmed")) as push:
            radio.relay_tick(self.conn)
            row = self.delivery(self.mid)
            self.assertEqual(row["status"], "pull")
            self.assertIsNone(row["focus_hold_since"])
            self.assertIsNotNone(row["last_attempt_at"])
            radio.relay_tick(self.conn)
            push.assert_called_once()
        self.assertEqual(self.delivery(self.mid)["status"], "pull")

    def test_agent_blocked_after_hold_remains_a_defer_without_consuming_an_attempt(self):
        """A confirmed blocked agent can later accept the named delivery without retry cost."""
        since = self.backdate_hold()
        with patch.object(radio, "push_to_pane", return_value=(False, "agent_blocked")) as push:
            radio.relay_tick(self.conn)
            push.assert_called_once()
        self.assert_deferred()
        self.assertEqual(self.delivery(self.mid)["focus_hold_since"], since)
        radio.relay_tick(self.conn)
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(self.delivery(self.mid)["status"], "delivered")
        self.assertEqual(self.delivery(self.mid)["attempts"], 0)
        self.assertIsNone(self.delivery(self.mid)["focus_hold_since"])


class FrequencyRelayBoundaryTest(FrequencyCase):
    def assert_raw_delivery_blocked(self, source, target, *, message_target=None):
        self.add_handle(scope=target)
        mid = radio.record_message(
            self.conn, "pm", "sender", "raw cross-scope body",
            to_handle="alice", from_ws=source,
            to_ws=target if message_target is None else message_target,
        )
        radio.enqueue_delivery(self.conn, mid, "alice", target)
        self.conn.commit()
        self.assertEqual(self.delivery(mid)["status"], "pending")

        radio.relay_tick(self.conn)

        self.assertEqual(self.pushed, [], (source, target, message_target))
        self.assertEqual(self.delivery(mid)["status"], "pull")
        self.assertFalse(self.conn.in_transaction)

    def test_named_source_cannot_push_into_default_workspace(self):
        self.assert_raw_delivery_blocked("freq.team", "w1")

    def test_default_source_cannot_push_into_named_frequency(self):
        self.assert_raw_delivery_blocked("w1", "freq.team")

    def test_named_source_cannot_push_into_legacy_global_scope(self):
        self.assert_raw_delivery_blocked("freq.team", "")

    def test_legacy_global_source_cannot_push_into_named_frequency(self):
        self.assert_raw_delivery_blocked("", "freq.team")

    def test_other_named_frequency_cannot_push_into_recipient_frequency(self):
        self.assert_raw_delivery_blocked("freq.other", "freq.team")

    def test_named_message_cannot_be_redirected_by_delivery_target(self):
        self.assert_raw_delivery_blocked("freq.team", "freq.team", message_target="freq.other")

    def test_message_named_destination_cannot_be_disguised_by_default_delivery_row(self):
        self.assert_raw_delivery_blocked("w1", "w1", message_target="freq.team")


if __name__ == "__main__":
    unittest.main()
