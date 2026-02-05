import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import current_app

logger = logging.getLogger(__name__)


def send_magic_link_email(to_email, magic_link_url):
    config = current_app.config
    sender = config.get("MAIL_DEFAULT_SENDER", "login@loore.org")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Your Write or Perish sign-in link"
    msg["From"] = sender
    msg["To"] = to_email

    text_body = (
        "Sign in to Write or Perish\n\n"
        f"Click the link below to sign in:\n{magic_link_url}\n\n"
        "This link expires in 15 minutes and can only be used once.\n\n"
        "If you didn't request this, you can safely ignore this email."
    )

    html_body = f"""\
<html>
<body style="font-family: -apple-system, sans-serif; background: #121212; color: #e0e0e0; padding: 40px;">
  <div style="max-width: 480px; margin: 0 auto; background: #1e1e1e; border-radius: 8px; border: 1px solid #333; padding: 40px;">
    <h2 style="margin-top: 0;">Sign in to Write or Perish</h2>
    <p>Click the button below to sign in:</p>
    <a href="{magic_link_url}"
       style="display: inline-block; padding: 12px 24px; background: #1DA1F2; color: white;
              text-decoration: none; border-radius: 4px; margin: 16px 0;">
      Sign in
    </a>
    <p style="color: #888; font-size: 14px; margin-top: 24px;">
      This link expires in 15 minutes and can only be used once.
    </p>
    <p style="color: #666; font-size: 12px;">
      If you didn't request this, you can safely ignore this email.
    </p>
  </div>
</body>
</html>"""

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    server = config.get("MAIL_SERVER", "localhost")
    port = config.get("MAIL_PORT", 587)
    use_tls = config.get("MAIL_USE_TLS", True)
    username = config.get("MAIL_USERNAME")
    password = config.get("MAIL_PASSWORD")

    try:
        with smtplib.SMTP(server, port) as smtp:
            if use_tls:
                smtp.starttls()
            if username and password:
                smtp.login(username, password)
            smtp.sendmail(sender, to_email, msg.as_string())
        logger.info(f"Magic link email sent to {to_email}")
    except Exception:
        logger.exception(f"Failed to send magic link email to {to_email}")
        raise
