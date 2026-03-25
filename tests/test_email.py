"""Unit tests for the email utility module (app/email.py)."""

from unittest.mock import patch, MagicMock

from app.email import is_email_configured, build_rankings_html, build_rankings_plain, send_rankings_email


SAMPLE_RANKINGS = [
    {"rank": 1, "contestant_name": "Alice", "tribe": "Cila", "tribe_color": "#e67e22"},
    {"rank": 2, "contestant_name": "Bob", "tribe": "Vatu", "tribe_color": "#2ecc71"},
    {"rank": 3, "contestant_name": "Charlie", "tribe": "Kalo", "tribe_color": "#9b59b6"},
]

TRIBE_COLORS = {"Cila": "#e67e22", "Vatu": "#2ecc71", "Kalo": "#9b59b6"}


class TestIsEmailConfigured:
    @patch("app.email.SMTP_EMAIL", "test@gmail.com")
    @patch("app.email.SMTP_PASSWORD", "app-password")
    def test_returns_true_when_configured(self):
        assert is_email_configured() is True

    @patch("app.email.SMTP_EMAIL", "")
    @patch("app.email.SMTP_PASSWORD", "")
    def test_returns_false_when_missing(self):
        assert is_email_configured() is False

    @patch("app.email.SMTP_EMAIL", "test@gmail.com")
    @patch("app.email.SMTP_PASSWORD", "")
    def test_returns_false_when_password_missing(self):
        assert is_email_configured() is False


class TestBuildRankingsHtml:
    def test_contains_season_name(self):
        html = build_rankings_html("Test User", "Season 50", SAMPLE_RANKINGS, "March 10, 2026")
        assert "Season 50" in html

    def test_contains_all_contestants(self):
        html = build_rankings_html("Test User", "Season 50", SAMPLE_RANKINGS, "March 10, 2026")
        assert "Alice" in html
        assert "Bob" in html
        assert "Charlie" in html

    def test_contains_tribe_colors(self):
        html = build_rankings_html("Test User", "Season 50", SAMPLE_RANKINGS, "March 10, 2026")
        assert "#e67e22" in html
        assert "#2ecc71" in html
        assert "#9b59b6" in html

    def test_contains_timestamp(self):
        html = build_rankings_html("Test User", "Season 50", SAMPLE_RANKINGS, "March 10, 2026")
        assert "March 10, 2026" in html

    def test_contains_predicted_winner_label(self):
        html = build_rankings_html("Test User", "Season 50", SAMPLE_RANKINGS, "March 10, 2026")
        assert "Predicted Winner" in html

    def test_contains_predicted_first_out_label(self):
        html = build_rankings_html("Test User", "Season 50", SAMPLE_RANKINGS, "March 10, 2026")
        assert "Predicted First Out" in html

    def test_contains_user_name(self):
        html = build_rankings_html("Test User", "Season 50", SAMPLE_RANKINGS, "March 10, 2026")
        assert "Test User" in html


class TestBuildRankingsPlain:
    def test_contains_all_data(self):
        plain = build_rankings_plain("Test User", "Season 50", SAMPLE_RANKINGS, "March 10, 2026")
        assert "Season 50" in plain
        assert "Alice" in plain
        assert "Predicted Winner" in plain
        assert "Predicted First Out" in plain


class TestSendRankingsEmail:
    @patch("app.email.SMTP_EMAIL", "")
    @patch("app.email.SMTP_PASSWORD", "")
    @patch("app.email.smtplib")
    def test_noop_when_not_configured(self, mock_smtplib):
        send_rankings_email("user@test.com", "User", "Season 50", SAMPLE_RANKINGS, TRIBE_COLORS)
        mock_smtplib.SMTP_SSL.assert_not_called()

    @patch("app.email.SMTP_EMAIL", "test@gmail.com")
    @patch("app.email.SMTP_PASSWORD", "app-password")
    @patch("app.email.smtplib")
    def test_calls_smtp_when_configured(self, mock_smtplib):
        mock_server = MagicMock()
        mock_smtplib.SMTP_SSL.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtplib.SMTP_SSL.return_value.__exit__ = MagicMock(return_value=False)

        send_rankings_email("user@test.com", "User", "Season 50", SAMPLE_RANKINGS, TRIBE_COLORS)

        mock_smtplib.SMTP_SSL.assert_called_once()
        mock_server.login.assert_called_once_with("test@gmail.com", "app-password")
        mock_server.send_message.assert_called_once()

    @patch("app.email.SMTP_EMAIL", "test@gmail.com")
    @patch("app.email.SMTP_PASSWORD", "app-password")
    @patch("app.email.smtplib")
    def test_swallows_exceptions(self, mock_smtplib):
        mock_smtplib.SMTP_SSL.side_effect = ConnectionRefusedError("Connection refused")
        # Should not raise
        send_rankings_email("user@test.com", "User", "Season 50", SAMPLE_RANKINGS, TRIBE_COLORS)
