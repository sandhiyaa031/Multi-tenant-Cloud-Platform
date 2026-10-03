"""Outbound mail. Development delivery is the server log; an SMTP sender can
replace `send` without touching callers."""
import logging

log = logging.getLogger("dbpilot.mail")


def send(to: str, subject: str, body: str) -> None:
    log.info("MAIL to=%s subject=%s\n%s", to, subject, body)
