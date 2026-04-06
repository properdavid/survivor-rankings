"""Email utility for sending rankings summaries and admin broadcasts."""

import logging
import re as _re
import smtplib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import SMTP_EMAIL, SMTP_PASSWORD, SMTP_HOST, SMTP_PORT

logger = logging.getLogger(__name__)


def is_email_configured() -> bool:
    return bool(SMTP_EMAIL and SMTP_PASSWORD)


def build_rankings_html(
    user_name: str,
    season_name: str,
    rankings: list[dict],
    timestamp: str,
) -> str:
    """Build an HTML email body for a user's rankings.

    Each item in rankings should have: rank, contestant_name, tribe, tribe_color.
    """
    total = len(rankings)
    rows = ""
    for r in rankings:
        tribe_color = r.get("tribe_color", "#666")
        label = ""
        if r["rank"] == 1:
            label = ' <span style="color:#d4a017;font-weight:bold;">(Predicted Winner)</span>'
        elif r["rank"] == total:
            label = ' <span style="color:#999;font-style:italic;">(Predicted First Out)</span>'

        rows += f"""
        <tr style="border-bottom:1px solid #eee;">
            <td style="padding:10px 12px;text-align:center;font-weight:bold;color:#555;">{r["rank"]}</td>
            <td style="padding:10px 12px;">{r["contestant_name"]}{label}</td>
            <td style="padding:10px 12px;">
                <span style="display:inline-block;padding:3px 10px;border-radius:12px;
                    background-color:{tribe_color};color:#fff;font-size:13px;">
                    {r.get("tribe", "")}
                </span>
            </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f5f5f5;">
    <div style="max-width:600px;margin:20px auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.1);">
        <div style="background:#e85d26;padding:24px;text-align:center;">
            <h1 style="margin:0;color:#fff;font-size:22px;">Your {season_name} Rankings</h1>
            <p style="margin:6px 0 0;color:rgba(255,255,255,0.85);font-size:14px;">for {user_name}</p>
        </div>
        <table style="width:100%;border-collapse:collapse;">
            <thead>
                <tr style="background:#f9f9f9;">
                    <th style="padding:10px 12px;text-align:center;font-size:13px;color:#888;width:50px;">#</th>
                    <th style="padding:10px 12px;text-align:left;font-size:13px;color:#888;">Contestant</th>
                    <th style="padding:10px 12px;text-align:left;font-size:13px;color:#888;">Tribe</th>
                </tr>
            </thead>
            <tbody>{rows}
            </tbody>
        </table>
        <div style="padding:16px 24px;text-align:center;color:#999;font-size:12px;border-top:1px solid #eee;">
            Saved on {timestamp} &bull; Survivor Rankings
        </div>
    </div>
</body>
</html>"""


def build_rankings_plain(
    user_name: str,
    season_name: str,
    rankings: list[dict],
    timestamp: str,
) -> str:
    """Build a plain-text fallback for the rankings email."""
    lines = [f"Your {season_name} Rankings", f"for {user_name}", ""]
    total = len(rankings)
    for r in rankings:
        suffix = ""
        if r["rank"] == 1:
            suffix = " (Predicted Winner)"
        elif r["rank"] == total:
            suffix = " (Predicted First Out)"
        tribe = r.get("tribe", "")
        lines.append(f"  {r['rank']}. {r['contestant_name']} [{tribe}]{suffix}")
    lines.append("")
    lines.append(f"Saved on {timestamp}")
    return "\n".join(lines)


def send_rankings_email(
    to_email: str,
    user_name: str,
    season_name: str,
    rankings: list[dict],
    tribe_colors: dict[str, str],
) -> None:
    """Send a rankings summary email. Silently no-ops if SMTP is not configured."""
    if not is_email_configured():
        return

    try:
        # Enrich rankings with tribe colors
        enriched = []
        for r in rankings:
            enriched.append({
                **r,
                "tribe_color": tribe_colors.get(r.get("tribe", ""), "#666"),
            })

        pacific = ZoneInfo("America/Los_Angeles")
        timestamp = datetime.now(pacific).strftime("%B %d, %Y at %I:%M %p %Z")

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Your {season_name} Rankings"
        msg["From"] = SMTP_EMAIL
        msg["To"] = to_email

        plain = build_rankings_plain(user_name, season_name, enriched, timestamp)
        html = build_rankings_html(user_name, season_name, enriched, timestamp)

        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.send_message(msg)

        logger.info("Rankings email sent to %s", to_email)
    except Exception:
        logger.exception("Failed to send rankings email to %s", to_email)


def build_broadcast_html(body_html: str) -> str:
    """Build a styled HTML email matching the site's dark/orange aesthetic.

    body_html is already HTML from the contenteditable editor.
    Caller must strip <script> tags before passing.
    """
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&display=swap" rel="stylesheet">
</head>
<body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0f0f1a;">
    <div style="max-width:600px;margin:20px auto;background:#1a1a2e;border-radius:12px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,0.3);">
        <a href="https://survivor.mendozaflix.com/" style="display:block;text-decoration:none;background:#1a1a2e;padding:20px 24px;">
            <div style="display:flex;align-items:center;gap:10px;">
                <span style="font-size:32px;line-height:1;">🔥</span>
                <span style="font-family:'Bebas Neue',sans-serif;font-size:32px;letter-spacing:3px;color:#fff;">SURVIVOR <span style="color:#e85d26;">RANKINGS</span></span>
            </div>
        </a>
        <div style="height:3px;background:linear-gradient(90deg,#e85d26,#f5a623);"></div>
        <div style="padding:28px 32px;color:#e8e8f0;font-size:15px;line-height:1.7;">
            {body_html}
        </div>
        <div style="padding:16px 24px;text-align:center;color:#8888a0;font-size:12px;border-top:1px solid #2a2a45;">
            <a href="https://survivor.mendozaflix.com/" style="color:#8888a0;text-decoration:underline;">Survivor Rankings</a> &bull; You are receiving this because you registered an account.
        </div>
    </div>
</body>
</html>"""


def send_broadcast_email(
    to_email: str,
    to_name: str,
    subject: str,
    body_html: str,
    body_text: str,
) -> None:
    """Send a broadcast email to a single recipient. No-ops if SMTP not configured."""
    if not is_email_configured():
        return
    try:
        safe_html = _re.sub(
            r"<script[^>]*>.*?</script>", "", body_html,
            flags=_re.DOTALL | _re.IGNORECASE,
        )
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_EMAIL
        msg["To"] = to_email
        msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(build_broadcast_html(safe_html), "html"))
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.send_message(msg)
        logger.info("Broadcast email sent to %s", to_email)
    except Exception:
        logger.exception("Failed to send broadcast email to %s", to_email)
