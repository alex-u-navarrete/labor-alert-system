"""
Notifier — handles email delivery via SendGrid.
Sends HTML emails with a clean layout. SMS removed; email-only.
"""

import logging
from html import escape

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

from config import Config

log = logging.getLogger(__name__)

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f5f5f5; margin: 0; padding: 20px; color: #1a1a1a; }}
  .card {{ background: #ffffff; border-radius: 8px; max-width: 600px;
           margin: 0 auto; overflow: hidden;
           box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
  .header {{ background: {header_color}; color: white; padding: 20px 24px; }}
  .header h1 {{ margin: 0; font-size: 18px; font-weight: 700; letter-spacing: 0.3px; }}
  .header p  {{ margin: 4px 0 0; font-size: 13px; opacity: 0.85; }}
  .section {{ padding: 16px 24px; border-bottom: 1px solid #f0f0f0; }}
  .section:last-child {{ border-bottom: none; }}
  .section-title {{ font-size: 11px; font-weight: 700; letter-spacing: 1px;
                    text-transform: uppercase; color: #888; margin: 0 0 10px; }}
  .metric-row {{ display: flex; gap: 24px; margin-bottom: 6px; }}
  .metric {{ flex: 1; }}
  .metric .val {{ font-size: 22px; font-weight: 700; color: {header_color}; }}
  .metric .lbl {{ font-size: 11px; color: #888; margin-top: 1px; }}
  .staff-row {{ display: flex; justify-content: space-between;
                padding: 7px 0; border-bottom: 1px solid #f8f8f8;
                font-size: 14px; }}
  .staff-row:last-child {{ border-bottom: none; }}
  .staff-name {{ font-weight: 600; }}
  .staff-cost {{ color: #888; }}
  .action-box {{ background: #fff8e6; border-left: 4px solid #f5a623;
                 padding: 10px 14px; border-radius: 4px; font-size: 14px;
                 font-weight: 600; margin-top: 10px; }}
  .item-row {{ font-size: 13px; padding: 3px 0; color: #444; }}
  .ai-section {{ background: #f0f7ff; padding: 16px 24px; }}
  .ai-title {{ font-size: 11px; font-weight: 700; letter-spacing: 1px;
               text-transform: uppercase; color: #2d7dd2; margin: 0 0 10px; }}
  .ai-body {{ font-size: 14px; line-height: 1.65; color: #1a1a1a; white-space: pre-wrap; }}
  .pace-line {{ font-size: 13px; color: #555; margin: 4px 0; }}
  .below {{ color: #c0392b; font-weight: 600; }}
  .above {{ color: #27ae60; font-weight: 600; }}
  .footer {{ text-align: center; font-size: 11px; color: #bbb;
             padding: 12px; }}
</style>
</head>
<body>
<div class="card">
{body}
</div>
<div class="footer">La Flor Blanca · Labor Alert System</div>
</body>
</html>
"""


class Notifier:
    """Sends alerts via SendGrid HTML email."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._sg     = SendGridAPIClient(config.sg_key)

    def send_email(self, subject: str, plain_body: str, html_body: str | None = None) -> None:
        for address in self._config.alert_emails:
            try:
                msg = Mail(from_email=self._config.email_from, to_emails=address, subject=subject)
                msg.add_content(plain_body, "text/plain")
                msg.add_content(html_body or _plain_to_html(subject, plain_body), "text/html")
                self._sg.send(msg)
                log.info("Email sent to %s", address)
            except Exception as exc:
                log.error("Failed email to %s: %s", address, exc)

    def send_alert(self, subject: str, body: str, html_body: str | None = None) -> None:
        self.send_email(subject, body, html_body)


def _plain_to_html(subject: str, text: str) -> str:
    """Convert plain text alert body to clean HTML email."""
    is_urgent   = "URGENT" in subject.upper()
    is_warning  = "WARNING" in subject.upper()
    header_color = "#c0392b" if is_urgent else "#e67e22" if is_warning else "#2c3e50"

    sections_html = []
    current_section_title = ""
    current_lines: list[str] = []

    def flush_section():
        nonlocal current_section_title, current_lines
        if not current_lines and not current_section_title:
            return
        content = _render_lines(current_lines, header_color)
        if content.strip():
            sections_html.append(
                f'<div class="section">'
                + (f'<div class="section-title">{escape(current_section_title)}</div>' if current_section_title else "")
                + content
                + "</div>"
            )
        current_section_title = ""
        current_lines = []

    lines = text.split("\n")
    header_lines: list[str] = []
    body_started = False
    ai_lines: list[str] = []
    in_ai = False

    for line in lines:
        stripped = line.strip()

        # Detect AI advisor section
        if "AI ADVISOR" in stripped and "─" not in stripped:
            flush_section()
            in_ai = True
            continue
        if in_ai:
            if stripped.startswith("─"):
                continue
            ai_lines.append(line)
            continue

        # Header (first two non-empty lines)
        if not body_started and stripped and not stripped.startswith("─"):
            if len(header_lines) < 2:
                header_lines.append(stripped)
                continue

        body_started = True

        # Section separators (─── TITLE ───)
        if stripped.startswith("─") and stripped.endswith("─") and len(stripped) > 4:
            flush_section()
            # Extract title from between the dashes
            title = stripped.strip("─").strip()
            current_section_title = title
            continue

        # Detect known section headers in all-caps
        if stripped and stripped == stripped.upper() and len(stripped) > 4 and ":" not in stripped and stripped.replace(" ", "").isalpha():
            flush_section()
            current_section_title = stripped
            continue

        current_lines.append(line)

    flush_section()

    # Build header block
    title = header_lines[0] if header_lines else subject
    subtitle = header_lines[1] if len(header_lines) > 1 else ""
    header_html = (
        f'<div class="header">'
        f'<h1>{escape(title)}</h1>'
        + (f'<p>{escape(subtitle)}</p>' if subtitle else "")
        + "</div>"
    )

    # Build AI section
    ai_html = ""
    ai_text = "\n".join(ai_lines).strip()
    if ai_text:
        ai_html = (
            '<div class="ai-section">'
            '<div class="ai-title">AI Advisor</div>'
            f'<div class="ai-body">{escape(ai_text)}</div>'
            "</div>"
        )

    body = header_html + "".join(sections_html) + ai_html
    return _HTML_TEMPLATE.format(body=body, header_color=header_color)


def _render_lines(lines: list[str], accent: str) -> str:
    html_parts = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("ACTION:"):
            html_parts.append(f'<div class="action-box">{escape(stripped)}</div>')
        elif ":" in stripped and stripped == stripped and len(stripped.split(":")[0]) < 25:
            html_parts.append(f'<div class="pace-line">{escape(stripped)}</div>')
        else:
            html_parts.append(f'<div class="pace-line">{escape(stripped)}</div>')
    return "".join(html_parts)
