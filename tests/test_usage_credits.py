"""Codex usage credits, rendering, and cached observations without provider I/O."""

import contextlib
from datetime import datetime, timezone
import json
import os
import unittest
from unittest.mock import patch

from test_radio import RadioTestCase, radio


_MISSING = object()


class UsageCreditsTest(RadioTestCase):
    """Only synthetic credentials and mocked GET responses enter these tests."""

    def setUp(self):
        super().setUp()
        self.home = self.tmp / "codex-fixture"
        self.home.mkdir()
        self.login("A")
        self.now_ts = 1800000000.0
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "CODEX_HOME": str(self.home),
            "KIMI_CODE_HOME": str(self.tmp / "missing-kimi"),
            "KIMI_HOME": str(self.tmp / "missing-kimi"),
        }))
        self.stack.enter_context(patch.object(radio, "claude_credentials", return_value=None))
        self.stack.enter_context(patch.object(radio.time, "time", side_effect=lambda: self.now_ts))
        self.stack.enter_context(patch.object(radio, "relay_lock", return_value=True))
        self.stack.enter_context(patch.object(
            radio.urllib.request, "urlopen", side_effect=AssertionError("unexpected network access")))
        self.http = self.stack.enter_context(patch.object(
            radio, "usage_http_get_json", side_effect=AssertionError("unexpected provider GET")))

    def login(self, account):
        """Replace this fixture home's login, including its cache identity."""
        (self.home / "auth.json").write_text(json.dumps({"tokens": {
            "access_token": "fixture-token-" + account,
            "account_id": "fixture-account-" + account,
        }}), encoding="utf-8")

    @staticmethod
    def response(credits=_MISSING, windows=True):
        payload = {"plan_type": "pro"}
        if windows:
            payload["rate_limit"] = {
                "primary_window": {"used_percent": 2, "limit_window_seconds": 18000},
                "secondary_window": {"used_percent": 39, "limit_window_seconds": 604800},
            }
        if credits is not _MISSING:
            payload["credits"] = credits
        return payload

    @staticmethod
    def credits(balance="62495.125", has_credits=True, unlimited=False):
        return {"has_credits": has_credits, "unlimited": unlimited, "balance": balance}

    def read(self, credits=_MISSING, windows=True):
        self.http.side_effect = None
        self.http.return_value = (200, self.response(credits, windows))
        return radio.read_codex_usage(self.home)

    def entry(self, value, provider="codex", observed_at=_MISSING):
        return {**value, "provider": provider,
                **({"observedAt": self.now_ts if observed_at is _MISSING else observed_at}
                   if observed_at is not None else {})}

    def assert_credit_rendering(self, entry, text):
        """Both public human renderers must expose the same credit state."""
        for render in (radio.render_usage_line, radio.render_usage_table):
            with self.subTest(renderer=render.__name__):
                rendered = render({"Codex": entry})
                self.assertIn(text, rendered)
                self.assertIn("credits", rendered.lower())

    def test_credits_and_quota_share_one_authenticated_get(self):
        value = self.read(self.credits())
        self.http.assert_called_once_with(radio.CODEX_USAGE_ENDPOINT, {
            "Authorization": "Bearer fixture-token-A",
            "ChatGPT-Account-Id": "fixture-account-A",
            "User-Agent": radio.USAGE_USER_AGENT,
        })
        self.assertEqual(value["plan"], "pro")
        self.assertEqual(value["credits"], {
            "hasCredits": True, "unlimited": False, "balance": "62495.125"})
        self.assertEqual([(window["label"], window["remainingPercent"])
                          for window in value["windows"]], [("5h", 98.0), ("Week", 61.0)])
        self.assert_credit_rendering(self.entry(value), "62,495.125 credits")
        self.assertIn("Week 61%", radio.render_usage_line({"Codex": self.entry(value)}))
        serialized = json.dumps(radio.usage_payload({"Codex": self.entry(value)}))
        self.assertNotIn("fixture-token-A", serialized)
        self.assertNotIn("fixture-account-A", serialized)

    def test_zero_fractional_and_long_decimal_balances_preserve_precision(self):
        cases = (
            ("0", "0 credits"),
            ("0.00000000", "0 credits"),
            ("62495.125", "62,495.125 credits"),
            ("1234567890123456789012345.1234567890123456789",
             "1,234,567,890,123,456,789,012,345.1234567890123456789 credits"),
        )
        for raw, expected in cases:
            with self.subTest(balance=raw):
                value = self.read(self.credits(raw, has_credits=raw != "0"))
                self.assertEqual(value["credits"]["balance"], raw)
                self.assertIsInstance(value["credits"]["balance"], str)
                entry = self.entry(value)
                self.assert_credit_rendering(entry, expected)
                account = radio.usage_payload({"Codex": entry})["accounts"][0]
                self.assertEqual(account["credits"]["balance"], raw)

    def test_unlimited_credits_have_explicit_text_without_a_numeric_balance(self):
        value = self.read(self.credits(None, has_credits=False, unlimited=True), windows=False)
        self.assertEqual(value["windows"], [])
        self.assertEqual(value["credits"], {
            "hasCredits": False, "unlimited": True, "balance": None})
        self.assert_credit_rendering(self.entry(value), "unlimited")
        account = radio.usage_payload({"Codex": self.entry(value)})["accounts"][0]
        self.assertEqual(account["credits"], value["credits"])

    def test_missing_null_and_malformed_flags_have_no_credit_snapshot(self):
        cases = (
            _MISSING, None, [], "bad", {},
            {"has_credits": True, "balance": "10"},
            {"unlimited": False, "balance": "10"},
            {"has_credits": None, "unlimited": False, "balance": "10"},
            {"has_credits": 1, "unlimited": False, "balance": "10"},
            {"has_credits": "true", "unlimited": False, "balance": "10"},
            {"has_credits": True, "unlimited": 0, "balance": "10"},
            {"has_credits": True, "unlimited": "false", "balance": "10"},
        )
        for raw in cases:
            with self.subTest(credits=raw):
                value = self.read(raw)
                self.assertIsNone(value["credits"])
                entry = self.entry(value)
                self.assert_credit_rendering(entry, "unavailable")
                self.assertIsNone(radio.usage_payload({"Codex": entry})["accounts"][0]["credits"])

    def test_invalid_balances_are_nullable_and_never_reach_terminal_output(self):
        cases = (None, "", " ", "bad", "NaN", "sNaN", "Inf", "Infinity", "-Infinity",
                 "1e999999", "9" * 10000, "\x1b[31m62495", "1\n2", True, 12, 12.5, [], {})
        for raw in cases:
            with self.subTest(balance=repr(raw)[:80]):
                value = self.read(self.credits(raw))
                self.assertEqual(value["credits"], {
                    "hasCredits": True, "unlimited": False, "balance": None})
                entry = self.entry(value)
                self.assert_credit_rendering(entry, "unavailable")
                for render in (radio.render_usage_line, radio.render_usage_table):
                    self.assertNotIn("\x1b", render({"Codex": entry}))
                account = radio.usage_payload({"Codex": entry})["accounts"][0]
                self.assertIsNone(account["credits"]["balance"])

    def test_credit_only_snapshot_renders_with_no_quota_windows(self):
        value = self.read(self.credits("12345.6789"), windows=False)
        self.assertEqual(value["windows"], [])
        entry = self.entry(value)
        self.assert_credit_rendering(entry, "12,345.6789 credits")
        account = radio.usage_payload({"Codex": entry})["accounts"][0]
        self.assertEqual(account["credits"]["balance"], "12345.6789")
        self.assertEqual(account["windows"], [])

    def test_non_codex_renderers_do_not_add_credit_text(self):
        entry = self.entry({"windows": [{"label": "Week", "remainingPercent": 80}],
                            "credits": {"hasCredits": True, "unlimited": False, "balance": "100"}},
                           provider="claude")
        for provider, label in (("claude", "Claude"), ("kimi", "Kimi")):
            entry["provider"] = provider
            for render in (radio.render_usage_line, radio.render_usage_table):
                with self.subTest(provider=provider, renderer=render.__name__):
                    self.assertNotIn("credits", render({label: entry}).lower())

    def test_hand_edited_cached_credits_are_sanitized_for_renderers_and_json(self):
        for credits in (
                {"hasCredits": True, "unlimited": False, "balance": "\x1b[31m100"},
                {"hasCredits": True, "unlimited": False, "balance": "NaN"},
                {"hasCredits": "true", "unlimited": False, "balance": "100"},
                "junk"):
            with self.subTest(credits=credits):
                entry = self.entry({"windows": [], "credits": credits})
                self.assert_credit_rendering(entry, "unavailable")
                for render in (radio.render_usage_line, radio.render_usage_table):
                    self.assertNotIn("\x1b", render({"Codex": entry}))
                normalized = radio.usage_payload({"Codex": entry})["accounts"][0]["credits"]
                if isinstance(credits, dict) and credits.get("hasCredits") is True:
                    self.assertEqual(normalized, {
                        "hasCredits": True, "unlimited": False, "balance": None})
                else:
                    self.assertIsNone(normalized)

    def test_each_account_reports_its_own_observation_age(self):
        old = self.now_ts - radio.USAGE_CACHE_TTL_S - 10
        entries = {
            "Codex": self.entry({"windows": [], "credits": {
                "hasCredits": True, "unlimited": False, "balance": "62495.125"}}, observed_at=old),
            "Claude": self.entry({"windows": [{"label": "Week", "remainingPercent": 90}]},
                                 provider="claude"),
            "Codex · missing": self.entry({"windows": [], "credits": None}, observed_at=None),
        }
        payload = radio.usage_payload(entries)
        accounts = {account["label"]: account for account in payload["accounts"]}
        self.assertEqual(accounts["Codex"]["observedAt"],
                         datetime.fromtimestamp(old, timezone.utc).isoformat())
        self.assertEqual(accounts["Codex"]["ageSeconds"], radio.USAGE_CACHE_TTL_S + 10)
        self.assertTrue(accounts["Codex"]["stale"])
        self.assertEqual(accounts["Claude"]["ageSeconds"], 0)
        self.assertFalse(accounts["Claude"]["stale"])
        self.assertIsNone(accounts["Codex · missing"]["observedAt"])
        self.assertIsNone(accounts["Codex · missing"]["ageSeconds"])
        self.assertTrue(accounts["Codex · missing"]["stale"])
        codex_segment = radio.render_usage_line(entries).split(" - ")[0]
        self.assertIn("62,495.125 credits", codex_segment)
        self.assertIn("stale", codex_segment.lower())
        table = radio.render_usage_table(entries)
        credit_row = next(line for line in table.splitlines() if "62,495.125" in line)
        self.assertIn("stale", credit_row.lower())

    def test_invalid_observation_times_are_unknown_and_stale_without_json_failure(self):
        for observed_at in (None, "yesterday", 0, -1, float("nan"), float("inf"),
                            float("-inf"), 1e300):
            with self.subTest(observed_at=observed_at):
                entries = {
                    "Codex": self.entry({"windows": [], "credits": {
                        "hasCredits": True, "unlimited": False, "balance": "100.125"}},
                        observed_at=observed_at),
                    "Claude": self.entry({"windows": []}, provider="claude"),
                }
                payload = radio.usage_payload(entries)
                account = payload["accounts"][0]
                self.assertIsNone(account["observedAt"])
                self.assertIsNone(account["ageSeconds"])
                self.assertTrue(account["stale"])
                self.assertFalse(payload["accounts"][1]["stale"])
                self.assertEqual(account["credits"]["balance"], "100.125")
                json.dumps(payload, allow_nan=False)
                for render in (radio.render_usage_line, radio.render_usage_table):
                    self.assertIn("100.125 credits", render(entries))

    def test_failed_credit_only_refresh_retains_observation_and_throttles_retry(self):
        for unlimited in (False, True):
            with self.subTest(unlimited=unlimited):
                radio.usage_cache_path().unlink(missing_ok=True)
                self.http.reset_mock()
                self.http.side_effect = [
                    (200, self.response(self.credits("12345.125", unlimited=unlimited), windows=False)),
                    (503, None),
                ]
                first = radio.refresh_usage_cache(self.conn)["Codex"]
                observed_at = self.now_ts
                self.assertEqual(first["observedAt"], observed_at)
                self.assertEqual(first["windows"], [])
                self.now_ts += radio.USAGE_CACHE_TTL_S + 10
                kept = radio.refresh_usage_cache(self.conn)["Codex"]
                self.assertEqual(kept["credits"], first["credits"])
                self.assertEqual(kept["observedAt"], observed_at)
                self.assertEqual(kept["attemptedAt"], self.now_ts)
                again = radio.refresh_usage_cache(self.conn)["Codex"]
                self.assertEqual(again, kept)
                self.assertEqual(self.http.call_count, 2)
                account = radio.usage_payload({"Codex": kept})["accounts"][0]
                self.assertTrue(account["stale"])
                self.assertEqual(account["ageSeconds"], radio.USAGE_CACHE_TTL_S + 10)
                self.assertEqual(radio.load_usage_cache()["Codex"], kept)

    def test_successful_refresh_without_credits_removes_previous_snapshot(self):
        self.http.side_effect = [
            (200, self.response(self.credits("100.125"))),
            (200, self.response()),
        ]
        first = radio.refresh_usage_cache(self.conn)["Codex"]
        self.assertEqual(first["credits"]["balance"], "100.125")
        self.now_ts += radio.USAGE_CACHE_TTL_S + 10
        refreshed = radio.refresh_usage_cache(self.conn)["Codex"]
        self.assertIsNone(refreshed["credits"])
        self.assertEqual(refreshed["observedAt"], self.now_ts)
        self.assertEqual(self.http.call_count, 2)
        self.assertNotIn("100.125", radio.render_usage_line({"Codex": refreshed}))
        self.assertIsNone(radio.usage_payload({"Codex": refreshed})["accounts"][0]["credits"])

    def test_login_switch_refreshes_credits_inside_ttl_and_does_not_cache_credentials(self):
        self.http.side_effect = [
            (200, self.response(self.credits("30.25"))),
            (200, self.response(self.credits("90.75"))),
        ]
        first = radio.refresh_usage_cache(self.conn)["Codex"]
        self.login("B")
        second = radio.refresh_usage_cache(self.conn)["Codex"]
        self.assertEqual(first["credits"]["balance"], "30.25")
        self.assertEqual(second["credits"]["balance"], "90.75")
        self.assertNotEqual(first["cacheIdentity"], second["cacheIdentity"])
        radio.refresh_usage_cache(self.conn)
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual([call.args[1]["ChatGPT-Account-Id"] for call in self.http.call_args_list],
                         ["fixture-account-A", "fixture-account-B"])
        cached = radio.usage_cache_path().read_text(encoding="utf-8")
        self.assertNotIn("30.25", cached)
        for secret in ("fixture-token-A", "fixture-token-B", "fixture-account-A", "fixture-account-B"):
            self.assertNotIn(secret, cached)

    def test_failed_new_login_does_not_inherit_previous_login_credits(self):
        self.http.side_effect = [
            (200, self.response(self.credits("30.25"), windows=False)),
            (503, None),
        ]
        radio.refresh_usage_cache(self.conn)
        self.login("B")
        second = radio.refresh_usage_cache(self.conn)["Codex"]
        account = radio.usage_payload({"Codex": second})["accounts"][0]
        self.assertIsNone(account["credits"])
        self.assertEqual(account["windows"], [])
        self.assertNotIn("30.25", radio.render_usage_line({"Codex": second}))
        radio.refresh_usage_cache(self.conn)
        self.assertEqual(self.http.call_count, 2)

    def test_login_change_during_get_discards_the_ambiguous_snapshot(self):
        def switch_login(url, headers):
            self.assertEqual(headers["ChatGPT-Account-Id"], "fixture-account-A")
            self.login("B")
            return 200, self.response(self.credits("30.25"), windows=False)

        self.http.side_effect = switch_login
        first = radio.refresh_usage_cache(self.conn)
        self.assertNotIn("Codex", first)
        self.assertNotIn("30.25", radio.usage_cache_path().read_text(encoding="utf-8"))
        self.http.side_effect = None
        self.http.return_value = (200, self.response(self.credits("90.75"), windows=False))
        second = radio.refresh_usage_cache(self.conn)["Codex"]
        self.assertEqual(second["credits"]["balance"], "90.75")
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(self.http.call_args.args[1]["ChatGPT-Account-Id"], "fixture-account-B")

    def test_discovered_target_does_not_read_a_different_login(self):
        target = next(target for target in radio.usage_targets(self.conn)
                      if target["label"] == "Codex")
        self.login("B")
        self.assertIsNone(radio.read_provider_usage(target))
        self.http.assert_not_called()
        self.login("A")
        self.http.side_effect = None
        self.http.return_value = (200, self.response(self.credits("30.25")))
        value = radio.read_provider_usage(target)
        self.assertEqual(value["credits"]["balance"], "30.25")
        self.http.assert_called_once()
        self.assertEqual(self.http.call_args.args[1]["ChatGPT-Account-Id"], "fixture-account-A")


if __name__ == "__main__":
    unittest.main()
