"""Message plugin: email delivery."""

import logging
import smtplib
import subprocess
from email.mime.text import MIMEText
from ... import config

log = logging.getLogger(__name__)


def send_message(contact: dict, subject: str, body: str) -> None:
    """Send an email notification."""
    to_addr = contact.get("email")
    if not to_addr:
        log.warning("email_plugin: no email address for contact %s",
                    contact.get("name", "?"))
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = "spong@localhost"
    msg["To"] = to_addr

    sendmail_cmd = config.get_command("sendmail", "/usr/sbin/sendmail -t")

    try:
        proc = subprocess.Popen(
            sendmail_cmd.split(),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.communicate(input=msg.as_bytes())
        if proc.returncode == 0:
            log.info("email sent to %s: %s", to_addr, subject)
        else:
            log.warning("sendmail returned %d for %s", proc.returncode, to_addr)
    except FileNotFoundError:
        # Fallback to Python smtplib
        try:
            with smtplib.SMTP("localhost") as smtp:
                smtp.send_message(msg)
            log.info("email sent to %s via smtplib", to_addr)
        except Exception as e:
            log.error("Failed to send email to %s: %s", to_addr, e)
    except Exception as e:
        log.error("email_plugin: %s", e)
