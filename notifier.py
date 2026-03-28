"""
Notifier — handles email delivery via SendGrid.
SMS has been removed; all alerts are email-only.
"""

import logging

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

from config import Config

log = logging.getLogger(__name__)


class Notifier:
    """Sends alerts via SendGrid email."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._sg     = SendGridAPIClient(config.sg_key)

    def send_email(self, subject: str, body: str) -> None:
        for address in self._config.alert_emails:
            try:
                msg = Mail(
                    from_email=self._config.email_from,
                    to_emails=address,
                    subject=subject,
                    plain_text_content=body,
                )
                self._sg.send(msg)
                log.info("Email sent to %s", address)
            except Exception as exc:
                log.error("Failed email to %s: %s", address, exc)

    def send_alert(self, subject: str, body: str) -> None:
        self.send_email(subject, body)
