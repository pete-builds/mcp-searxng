"""Tests for healthcheck.py."""

from unittest.mock import MagicMock, patch

import pytest

from healthcheck import check


class TestHealthCheck:
    def test_healthy_exits_0(self):
        mock_resp = MagicMock()
        mock_resp.status = 200
        with (
            patch("healthcheck.urllib.request.urlopen", return_value=mock_resp),
            pytest.raises(SystemExit) as exc_info,
        ):
            check()
        assert exc_info.value.code == 0

    def test_non_200_exits_1(self):
        mock_resp = MagicMock()
        mock_resp.status = 503
        with (
            patch("healthcheck.urllib.request.urlopen", return_value=mock_resp),
            pytest.raises(SystemExit) as exc_info,
        ):
            check()
        assert exc_info.value.code == 1

    def test_connection_error_exits_1(self):
        with (
            patch(
                "healthcheck.urllib.request.urlopen",
                side_effect=ConnectionRefusedError("refused"),
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            check()
        assert exc_info.value.code == 1

    def test_timeout_exits_1(self):
        from urllib.error import URLError

        with (
            patch(
                "healthcheck.urllib.request.urlopen",
                side_effect=URLError("timeout"),
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            check()
        assert exc_info.value.code == 1
