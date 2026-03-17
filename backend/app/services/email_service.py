"""
Email service for sending HTML emails with optional attachments.

Adapted from the standalone email_utils.py used by D08 report scripts.
Uses Settings for SMTP config instead of direct env access.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email import encoders
from email.header import make_header
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Optional, Union

from ..core.config import get_settings

logger = logging.getLogger(__name__)


# Attachment can be a file path string or a dict with 'filepath' and optional 'cid' (for inline images)
Attachment = Union[str, dict]


def send_email(
    subject: str,
    body: str,
    to: str,
    cc: Optional[str] = None,
    attachments: Optional[List[Attachment]] = None,
) -> None:
    """Send an HTML email via SMTP with optional attachments.

    Args:
        subject: Email subject line.
        body: HTML body content.
        to: Comma-separated recipient addresses.
        cc: Comma-separated CC addresses.
        attachments: List of file paths or dicts with 'filepath'/'cid' keys.

    Raises:
        RuntimeError: If SMTP credentials are not configured.
        smtplib.SMTPException: On SMTP-level failures.
    """
    settings = get_settings()

    if not settings.SMTP_USERNAME or not settings.SMTP_PASSWORD:
        raise RuntimeError("SMTP credentials not configured in .env")

    msg = MIMEMultipart("related")
    msg["From"] = settings.SMTP_USERNAME
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject

    msg.attach(MIMEText(body, "html"))

    for attachment in (attachments or []):
        if isinstance(attachment, dict) and "cid" in attachment:
            # Inline image referenced by Content-ID in HTML body
            with open(attachment["filepath"], "rb") as f:
                img = MIMEImage(f.read())
                img.add_header("Content-ID", f"<{attachment['cid']}>")
                img.add_header(
                    "Content-Disposition",
                    "inline",
                    filename=os.path.basename(attachment["filepath"]),
                )
                msg.attach(img)
        else:
            path = attachment["filepath"] if isinstance(attachment, dict) else attachment
            with open(path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
                encoders.encode_base64(part)
                filename = make_header([(os.path.basename(path), "utf-8")]).encode()
                part.add_header("Content-Disposition", "attachment", filename=filename)
                msg.attach(part)

    to_list = [addr.strip() for addr in to.split(",") if addr.strip()]
    cc_list = [addr.strip() for addr in cc.split(",") if addr.strip()] if cc else []
    all_recipients = to_list + cc_list

    server = smtplib.SMTP(settings.SMTP_SERVER, settings.SMTP_PORT)
    try:
        server.starttls()
        server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        server.sendmail(msg["From"], all_recipients, msg.as_string())
        logger.info(f"Email sent: subject='{subject}', to={to_list}, cc={cc_list}")
    finally:
        server.quit()


def send_verification_code(email: str, code: str) -> None:
    """Send a 6-digit verification code to an admin email.

    Used by the IB Financial Monitor page to verify config changes.
    """
    subject = "IB Financial Monitor - Verification Code"
    body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 480px; margin: 0 auto; padding: 24px;">
        <h2 style="color: #1a1a1a;">Verification Code</h2>
        <p>Your verification code for IB Financial Monitor operation:</p>
        <div style="font-size: 32px; font-weight: bold; letter-spacing: 8px;
                    color: #2563eb; padding: 16px; background: #f0f4ff;
                    border-radius: 8px; text-align: center; margin: 16px 0;">
            {code}
        </div>
        <p style="color: #666; font-size: 13px;">
            This code expires in 5 minutes. If you did not request this, please ignore.
        </p>
    </div>
    """
    send_email(subject=subject, body=body, to=email)
