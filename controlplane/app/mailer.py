"""Outbound mail.

With SMTP_HOST set, mail is sent through that server. Without it (the
development and demo set-up) nothing leaves the machine: the message is written
to the server log, and the API hands links to an organization's admin instead of
pretending they were delivered (see `configured`).
"""
import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger("dbpilot.mail")


def configured() -> bool:
    return bool(os.environ.get("SMTP_HOST"))


def send(to: str, subject: str, body: str) -> None:
    if not configured():
        log.info("MAIL (not delivered: no SMTP_HOST) to=%s subject=%s\n%s", to, subject, body)
        return
    message = EmailMessage()
    message["From"] = os.environ.get("SMTP_FROM", "dbpilot@localhost")
    message["To"], message["Subject"] = to, subject
    message.set_content(body)
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "25")), timeout=10) as smtp:
        if os.environ.get("SMTP_USER"):
            smtp.starttls()
            smtp.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        smtp.send_message(message)
