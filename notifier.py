"""
Notifier — handles SMS (Twilio) and email (SendGrid) delivery.
"""

import logging

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from twilio.rest import Client as TwilioClient

from config import Config

log = logging.getLogger(__name__)


class Notifier:
    """Sends alerts via SMS and/or email depending on configuration."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._twilio = TwilioClient(config.twilio_sid, config.twilio_token)
        self._sg     = SendGridAPIClient(config.sg_key) if config.sendgrid_enabled else None

    def send_sms(self, message: str) -> None:
        for phone in self._config.alert_phones:
            try:
                self._twilio.messages.create(
                    body=message,
                    from_=self._config.twilio_from,
                    to=phone,
                )
                log.info("SMS sent to %s", phone)
            except Exception as exc:
                log.error("Failed SMS to %s: %s", phone, exc)

    def send_email(self, subject: str, body: str) -> None:
        """No-op if SendGrid is not configured."""
        if not self._config.sendgrid_enabled:
            return
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

    def send_alert(self, subject: str, sms_body: str, email_body: str | None = None) -> None:
        """Fire SMS and (if configured) email. email_body falls back to sms_body."""
        self.send_sms(sms_body)
        self.send_email(subject, email_body if email_body is not None else sms_body)
