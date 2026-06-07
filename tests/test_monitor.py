from __future__ import annotations

import contextlib
import io
import os
import sys
import types
import unittest
from unittest import mock

import monitor


class MonitorTestCase(unittest.TestCase):
    def make_config(self, auth_alert_sent: str = "0", cursor: int = 1) -> monitor.Config:
        return monitor.Config(
            source_token="synthetic-source-token",
            monitored_account="syntheticacct",
            discord_webhook="https://discord.com/api/webhooks/0/synthetic-webhook-secret",
            cursor=cursor,
            repository="synthetic-owner/synthetic-repo",
            gh_token="synthetic-gh-token",
            auth_alert_sent=auth_alert_sent,
        )


class AuthenticateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.original_tweety = sys.modules.get("tweety")
        self.original_session = sys.modules.get("tweety.session")

    async def asyncTearDown(self) -> None:
        if self.original_tweety is None:
            sys.modules.pop("tweety", None)
        else:
            sys.modules["tweety"] = self.original_tweety
        if self.original_session is None:
            sys.modules.pop("tweety.session", None)
        else:
            sys.modules["tweety.session"] = self.original_session

    async def test_authentication_is_attempted_three_times_and_raises_authentication_error(self) -> None:
        attempts = []

        class FakeTwitterAsync:
            def __init__(self, *args, **kwargs):
                self.request = types.SimpleNamespace(
                    session=types.SimpleNamespace(aclose=mock.AsyncMock())
                )

            async def load_auth_token(self, source_token):
                attempts.append(source_token)
                raise RuntimeError("synthetic auth failure")

        sys.modules["tweety"] = types.SimpleNamespace(TwitterAsync=FakeTwitterAsync)
        sys.modules["tweety.session"] = types.SimpleNamespace(MemorySession=lambda: object())

        with mock.patch.object(monitor.asyncio, "sleep", mock.AsyncMock()) as sleep:
            with self.assertRaises(monitor.AuthenticationError):
                await monitor.authenticate("synthetic-source-token")

        self.assertEqual(attempts, ["synthetic-source-token"] * 3)
        self.assertEqual(sleep.await_count, 2)


class ProcessAuthenticationAlertTests(MonitorTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_authentication_failure_with_state_zero_sends_warning_once_and_sets_state(self) -> None:
        events = []

        async def fake_deliver(webhook, payload):
            events.append(("deliver", webhook, payload))

        def fake_update(state, repository, gh_token):
            events.append(("update", state, repository, gh_token))

        with mock.patch.object(monitor, "authenticate", mock.AsyncMock(side_effect=monitor.AuthenticationError())):
            with mock.patch.object(monitor, "deliver_discord", fake_deliver):
                with mock.patch.object(monitor, "update_auth_alert_secret", fake_update):
                    with self.assertRaises(monitor.AuthenticationError):
                        await monitor.process(self.make_config("0"))

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0][0], "deliver")
        self.assertEqual(events[0][2]["content"], monitor.AUTH_ALERT_MESSAGE)
        self.assertEqual(events[0][2]["allowed_mentions"], {"parse": []})
        self.assertEqual(events[1], ("update", "1", "synthetic-owner/synthetic-repo", "synthetic-gh-token"))

    async def test_authentication_failure_with_state_one_sends_no_duplicate_warning(self) -> None:
        with mock.patch.object(monitor, "authenticate", mock.AsyncMock(side_effect=monitor.AuthenticationError())):
            with mock.patch.object(monitor, "deliver_discord", mock.AsyncMock()) as deliver:
                with mock.patch.object(monitor, "update_auth_alert_secret") as update:
                    with self.assertRaises(monitor.AuthenticationError):
                        await monitor.process(self.make_config("1"))

        deliver.assert_not_awaited()
        update.assert_not_called()

    async def test_successful_authentication_resets_state_one_to_zero(self) -> None:
        app = object()
        timeline = monitor.TimelineResult([], 1, False, False)
        with mock.patch.object(monitor, "authenticate", mock.AsyncMock(return_value=app)):
            with mock.patch.object(monitor, "retrieve_timeline", mock.AsyncMock(return_value=timeline)):
                with mock.patch.object(monitor, "update_auth_alert_secret") as update:
                    with mock.patch.object(monitor, "close_tweety_client", mock.AsyncMock()):
                        await monitor.process(self.make_config("1", cursor=1))

        update.assert_called_once_with("0", "synthetic-owner/synthetic-repo", "synthetic-gh-token")

    async def test_successful_authentication_with_state_zero_does_not_reset(self) -> None:
        app = object()
        timeline = monitor.TimelineResult([], 1, False, False)
        with mock.patch.object(monitor, "authenticate", mock.AsyncMock(return_value=app)):
            with mock.patch.object(monitor, "retrieve_timeline", mock.AsyncMock(return_value=timeline)):
                with mock.patch.object(monitor, "update_auth_alert_secret") as update:
                    with mock.patch.object(monitor, "close_tweety_client", mock.AsyncMock()):
                        await monitor.process(self.make_config("0", cursor=1))

        update.assert_not_called()

    async def test_non_authentication_failures_never_send_authentication_warning(self) -> None:
        app = object()
        with mock.patch.object(monitor, "authenticate", mock.AsyncMock(return_value=app)):
            with mock.patch.object(monitor, "retrieve_timeline", mock.AsyncMock(side_effect=monitor.MonitorError())):
                with mock.patch.object(monitor, "deliver_discord", mock.AsyncMock()) as deliver:
                    with mock.patch.object(monitor, "close_tweety_client", mock.AsyncMock()):
                        with self.assertRaises(monitor.MonitorError):
                            await monitor.process(self.make_config("0", cursor=1))

        deliver.assert_not_awaited()


class ConfigTests(MonitorTestCase):
    def test_invalid_auth_alert_values_fail_safely(self) -> None:
        for value in (None, "", " ", "2", "false", " 0 "):
            with self.subTest(value=value):
                with self.assertRaises(monitor.ConfigError):
                    monitor.parse_auth_alert_state(value)

    def test_load_config_reads_valid_auth_alert_state(self) -> None:
        env = {
            "X_SOURCE_TOKEN": "synthetic-source-token",
            "GH_TOKEN": "synthetic-gh-token",
            "DISCORD_WEBHOOK": "https://discord.com/api/webhooks/0/synthetic-webhook-secret",
            "GITHUB_REPOSITORY": "synthetic-owner/synthetic-repo",
            "X_MONITORED_ACCOUNT": "syntheticacct",
            "X_POST_ID": "0",
            "X_AUTH_ALERT_SENT": "1",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            config = monitor.load_config()
        self.assertEqual(config.auth_alert_sent, "1")


class OutputHardeningTests(unittest.TestCase):
    def test_main_output_is_sanitized_for_success_and_failure(self) -> None:
        for async_main, expected_code, expected_output in (
            (mock.AsyncMock(return_value=None), 0, f"{monitor.SUCCESS_LINE}\n"),
            (mock.AsyncMock(side_effect=RuntimeError("synthetic private detail")), 1, f"{monitor.FAILURE_LINE}\n"),
        ):
            with self.subTest(expected_code=expected_code):
                stdout = io.StringIO()
                with mock.patch.object(monitor, "async_main", async_main):
                    with contextlib.redirect_stdout(stdout):
                        code = monitor.main()
                self.assertEqual(code, expected_code)
                self.assertEqual(stdout.getvalue(), expected_output)
                self.assertNotIn("synthetic private detail", stdout.getvalue())

    def test_environment_secret_update_does_not_pass_secret_value_in_command_arguments(self) -> None:
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["input"] = kwargs["input"]
            return types.SimpleNamespace(returncode=0)

        with mock.patch.object(monitor.subprocess, "run", fake_run):
            monitor.update_environment_secret(
                "X_AUTH_ALERT_SENT",
                "1",
                "synthetic-owner/synthetic-repo",
                "synthetic-gh-token",
            )

        self.assertNotIn("1", captured["command"])
        self.assertEqual(captured["input"], "1\n")


if __name__ == "__main__":
    unittest.main()
