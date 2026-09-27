#!/usr/bin/env python3
"""Hermetic Codex rollout parser tests; no user sessions or processes are read."""

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parent.parent
_loader = importlib.machinery.SourceFileLoader("radio_context_reader", str(REPO / "bin" / "radio"))
_spec = importlib.util.spec_from_file_location("radio_context_reader", REPO / "bin" / "radio", loader=_loader)
radio = importlib.util.module_from_spec(_spec)
with tempfile.TemporaryDirectory(prefix="radio-context-import-") as _state:
    with patch.dict(os.environ, {"RADIO_HOME": _state}):
        _spec.loader.exec_module(radio)


class ContextReaderTests(unittest.TestCase):
    SESSION = "11111111-2222-4333-8444-555555555555"
    TIME = "2026-09-27T12:00:00Z"

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="radio-context-test-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "rollout.jsonl"

    def meta(self, session=None):
        return {"type": "session_meta", "payload": {"id": session or self.SESSION}}

    def turn(self, model="gpt-6-luna", effort="xhigh"):
        return {"type": "turn_context", "timestamp": self.TIME,
                "payload": {"model": model, "effort": effort}}

    def sample(self, tokens=24000, capacity=128000, stamp=None):
        return {"type": "event_msg", "timestamp": stamp or self.TIME,
                "payload": {"type": "token_count", "info": {
                    "last_token_usage": {"total_tokens": tokens},
                    "total_token_usage": {"total_tokens": 9999999},
                    "model_context_window": capacity}}}

    def write(self, *events):
        self.path.write_bytes(b"".join(
            (event if isinstance(event, str) else json.dumps(event)).encode("utf-8") + b"\n"
            for event in events))

    def read(self):
        return radio.read_codex_context(self.path, self.SESSION)

    def assert_empty(self, result, error):
        self.assertEqual(result, radio._empty_codex_context(error))

    def test_last_response_not_cumulative_usage_and_no_transcript_fields(self):
        self.write(self.meta(), self.turn(), self.sample(),
                   {"type": "response_item", "payload": {"text": "PRIVATE_TRANSCRIPT",
                    "access_token": "PRIVATE_CREDENTIAL"}})
        result = self.read()
        self.assertEqual(result, {"model": "gpt-6-luna", "effort": "xhigh",
                                 "context_tokens": 24000, "context_window": 128000,
                                 "observed_at": self.TIME, "error": None})
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_filename_is_not_session_identity(self):
        self.write(self.turn(), self.sample())
        self.assert_empty(self.read(), "session_unverified")
        self.write(self.meta("another-session"), self.turn(), self.sample())
        self.assert_empty(self.read(), "session_mismatch")

    def test_forked_session_accepts_only_its_immediate_parent_meta(self):
        parent = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        child = self.meta()
        child["payload"]["forked_from_id"] = parent
        self.write(child, self.meta(parent), self.turn(), self.sample())
        self.assertEqual(self.read()["context_tokens"], 24000)
        self.write(child, self.turn(), self.meta(parent), self.sample())
        self.assert_empty(self.read(), "session_mismatch")
        self.write(child, self.meta("another-session"), self.turn(), self.sample())
        self.assert_empty(self.read(), "session_mismatch")

    def test_records_before_session_meta_are_never_consumed(self):
        self.write(self.turn(), self.sample(), self.meta())
        self.assert_empty(self.read(), None)

    def test_later_mismatch_discards_prior_metadata(self):
        self.write(self.meta(), self.turn(), self.sample(), self.meta("another-session"))
        self.assert_empty(self.read(), "session_mismatch")

    def test_non_string_session_id_is_rejected(self):
        for session in (None, 12, "", "bad\x1b[2J", [self.SESSION]):
            with self.subTest(session=session):
                self.assert_empty(radio.read_codex_context(self.path, session), "invalid_session_id")

    def test_latest_sample_replaces_previous_and_zero_is_valid(self):
        self.write(self.meta(), self.turn(), self.sample(), self.sample(tokens=0))
        self.assertEqual(self.read()["context_tokens"], 0)

    def test_rate_only_event_keeps_previous_sample(self):
        self.write(self.meta(), self.turn(), self.sample(),
                   {"type": "event_msg", "payload": {"type": "token_count", "info": None}})
        self.assertEqual(self.read()["context_tokens"], 24000)

    def test_compaction_clears_sample_until_next_measurement(self):
        for event in ({"type": "compacted", "payload": {}},
                      {"type": "event_msg", "payload": {"type": "context_compacted"}},
                      {"type": "event_msg", "payload": {"type": "contextCompaction"}}):
            with self.subTest(event=event):
                self.write(self.meta(), self.turn(), self.sample(), event)
                result = self.read()
                self.assertIsNone(result["context_tokens"])
                self.assertIsNone(result["context_window"])
                self.assertIsNone(result["observed_at"])
                self.assertEqual(result["model"], "gpt-6-luna")
                self.write(self.meta(), self.turn(), self.sample(), event, self.sample(tokens=100))
                self.assertEqual(self.read()["context_tokens"], 100)

    def test_model_change_clears_sample_but_effort_change_does_not(self):
        self.write(self.meta(), self.turn(), self.sample(), self.turn(effort="high"))
        self.assertEqual(self.read()["context_tokens"], 24000)
        self.write(self.meta(), self.turn(), self.sample(), self.turn(model="gpt-6-sol"))
        result = self.read()
        self.assertEqual(result["model"], "gpt-6-sol")
        self.assertIsNone(result["context_tokens"])
        self.assertIsNone(result["observed_at"])

    def test_model_and_effort_are_safe_bounded_strings(self):
        for model, effort in (({}, []), ("gpt\x1b[2J", "xhigh\n"), ("x" * 129, "x" * 33)):
            self.write(self.meta(), self.turn(model=model, effort=effort))
            result = self.read()
            self.assertIsNone(result["model"])
            self.assertIsNone(result["effort"])

    def test_unknown_capacity_does_not_invent_percentage_input(self):
        for capacity in (None, 0, -1, True, 1.5, "128000", 2**64):
            with self.subTest(capacity=capacity):
                self.write(self.meta(), self.turn(), self.sample(capacity=capacity))
                result = self.read()
                self.assertEqual(result["context_tokens"], 24000)
                self.assertIsNone(result["context_window"])

    def test_invalid_counter_discards_entire_sample(self):
        for tokens in (None, -1, True, 1.5, "24000", 2**64):
            with self.subTest(tokens=tokens):
                self.write(self.meta(), self.turn(), self.sample(), self.sample(tokens=tokens))
                result = self.read()
                self.assertIsNone(result["context_tokens"])
                self.assertIsNone(result["context_window"])
                self.assertIsNone(result["observed_at"])
                self.assertEqual(result["error"], "invalid_sample")

    def test_sample_requires_timezone_aware_timestamp(self):
        for stamp in ("not-a-date", "2026-09-27T12:00:00", 123, {}):
            with self.subTest(stamp=stamp):
                event = self.sample()
                event["timestamp"] = stamp
                self.write(self.meta(), self.turn(), event)
                self.assertEqual(self.read()["error"], "invalid_sample")
                self.assertIsNone(self.read()["observed_at"])
        self.write(self.meta(), self.sample(stamp="2026-09-27T15:00:00+03:00"))
        self.assertEqual(self.read()["observed_at"], self.TIME)

    def test_malformed_complete_records_clear_sample_and_later_sample_recovers(self):
        for broken in ("{bad json", "[]", "null", '{"type":"event_msg","payload":null}'):
            with self.subTest(broken=broken):
                self.write(self.meta(), self.turn(), self.sample(), broken)
                result = self.read()
                self.assertIsNone(result["context_tokens"])
                self.assertIsNone(result["observed_at"])
                self.assertEqual(result["error"], "invalid_record")
                self.write(self.meta(), self.turn(), self.sample(), broken, self.sample(tokens=100))
                self.assertEqual(self.read()["context_tokens"], 100)
                self.assertIsNone(self.read()["error"])

    def test_invalid_info_and_missing_last_usage_do_not_keep_previous_count(self):
        for info in ([], "invalid", {}, {"total_token_usage": {"total_tokens": 12345}}):
            self.write(self.meta(), self.turn(), self.sample(),
                       {"type": "event_msg", "payload": {"type": "token_count", "info": info}})
            result = self.read()
            self.assertIsNone(result["context_tokens"])
            self.assertEqual(result["error"], "invalid_sample")

    def test_partial_tail_is_ignored(self):
        self.write(self.meta(), self.turn(), self.sample())
        with self.path.open("ab") as stream:
            stream.write(b'{"type":"compacted"')
        self.assertEqual(self.read()["context_tokens"], 24000)

    def test_file_and_line_limits_are_enforced(self):
        self.write(self.meta(), self.turn(), self.sample())
        with patch.object(radio, "CONTEXT_MAX_FILE_BYTES", 16):
            self.assert_empty(self.read(), "file_too_large")
        self.write(self.meta(), self.turn(), self.sample(),
                   {"type": "response_item", "payload": {"text": "PRIVATE" * 300}})
        with patch.object(radio, "CONTEXT_MAX_LINE_BYTES", 512):
            result = self.read()
            self.assertEqual(result["error"], "line_too_large")
            self.assertIsNone(result["context_tokens"])
            self.assertIsNone(result["context_window"])
            self.assertIsNone(result["observed_at"])
            self.assertNotIn("PRIVATE", json.dumps(result))

    def test_large_tool_record_is_skipped_and_later_measurement_recovers(self):
        self.write(self.meta(), self.turn(), self.sample(),
                   {"type": "response_item", "payload": {"text": "PRIVATE" * 300}},
                   self.sample(tokens=11000))
        with patch.object(radio, "CONTEXT_MAX_LINE_BYTES", 512):
            result = self.read()
        self.assertIsNone(result["model"])
        self.assertIsNone(result["effort"])
        self.assertEqual(result["context_tokens"], 11000)
        self.assertEqual(result["observed_at"], self.TIME)
        self.assertIsNone(result["error"])

    def test_unreadable_turn_cannot_attribute_new_sample_to_previous_model(self):
        large_turn = self.turn(model="gpt-6-sol", effort="high")
        large_turn["payload"]["instructions"] = "x" * 1500
        for unreadable in (large_turn, '{"type":"turn_context","payload":'):
            with self.subTest(unreadable=type(unreadable).__name__):
                events = (self.meta(), self.turn(), self.sample(), unreadable,
                          self.sample(tokens=11000))
                self.write(*events)
                with patch.object(radio, "CONTEXT_MAX_LINE_BYTES", 512):
                    result = self.read()
                self.assertEqual(result["context_tokens"], 11000)
                self.assertIsNone(result["model"])
                self.assertIsNone(result["effort"])
                self.write(*events, self.turn(model="gpt-6-sol", effort="high"),
                           self.sample(tokens=12000))
                with patch.object(radio, "CONTEXT_MAX_LINE_BYTES", 512):
                    result = self.read()
                self.assertEqual(result["model"], "gpt-6-sol")
                self.assertEqual(result["effort"], "high")
                self.assertEqual(result["context_tokens"], 12000)

    def test_large_partial_tail_discards_previous_sample(self):
        self.write(self.meta(), self.turn(), self.sample())
        with self.path.open("ab") as stream:
            stream.write(b'{"type":"response_item","payload":{"text":"' + b"x" * 1500)
        with patch.object(radio, "CONTEXT_MAX_LINE_BYTES", 512):
            result = self.read()
        self.assertEqual(result["error"], "line_too_large")
        self.assertIsNone(result["context_tokens"])
        self.assertIsNone(result["observed_at"])

    def test_oversized_header_never_verifies_session_identity(self):
        header = self.meta()
        header["payload"]["metadata"] = "x" * 1500
        self.write(header, self.turn(), self.sample())
        with patch.object(radio, "CONTEXT_MAX_LINE_BYTES", 512):
            self.assert_empty(self.read(), "session_unverified")

    def test_file_unavailable_returns_sanitized_code_without_path(self):
        self.assert_empty(self.read(), "file_unavailable")
        self.path.mkdir()
        self.assert_empty(self.read(), "file_unavailable")

    def test_file_change_during_read_discards_snapshot(self):
        self.write(self.meta(), self.turn(), self.sample())
        initial = self.path.stat()
        for field, changed_value in (("st_size", initial.st_size - 1),
                                     ("st_size", initial.st_size + 1),
                                     ("st_mtime_ns", initial.st_mtime_ns + 1),
                                     ("st_ino", initial.st_ino + 1)):
            changed = types.SimpleNamespace(**{
                name: getattr(initial, name)
                for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")})
            setattr(changed, field, changed_value)
            with self.subTest(field=field, value=changed_value):
                with patch.object(radio.os, "fstat", side_effect=[initial, changed]):
                    self.assert_empty(self.read(), "file_changed")

    def test_no_incremental_state_survives_file_replacement(self):
        self.write(self.meta(), self.turn(), self.sample())
        self.assertEqual(self.read()["context_tokens"], 24000)
        self.write(self.meta(), self.turn(model="gpt-6-sol"))
        result = self.read()
        self.assertEqual(result["model"], "gpt-6-sol")
        self.assertIsNone(result["context_tokens"])


if __name__ == "__main__":
    unittest.main()
