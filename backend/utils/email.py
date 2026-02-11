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
    msg["Subject"] = "Your Loore sign-in link"
    msg["From"] = sender
    msg["To"] = to_email

    text_body = (
        "Sign in to Loore\n\n"
        f"Click the link below to sign in:\n{magic_link_url}\n\n"
        "This link expires in 15 minutes and can only be used once.\n\n"
        "If you didn't request this, you can safely ignore this email."
    )

    html_body = f"""\
<html>
<body style="font-family: 'Outfit', -apple-system, sans-serif; background: #0e0d0b; color: #ede8dd; padding: 40px 20px; margin: 0;">
  <div style="max-width: 460px; margin: 0 auto; background: #181714; border-radius: 10px; border: 1px solid #302c27; padding: 48px 40px;">
    <div style="font-family: 'Cormorant Garamond', Georgia, serif; font-size: 14px; font-weight: 300; text-transform: uppercase; letter-spacing: 0.3em; color: #736b5f; margin-bottom: 32px;">
      Loore
    </div>
    <h2 style="font-family: 'Cormorant Garamond', Georgia, serif; font-weight: 300; font-size: 28px; color: #ede8dd; margin: 0 0 12px 0;">
      Sign in
    </h2>
    <p style="font-size: 15px; font-weight: 300; color: #a89f91; margin: 0 0 28px 0; line-height: 1.6;">
      Click the button below to continue to your account.
    </p>
    <a href="{magic_link_url}"
       style="display: inline-block; padding: 12px 32px; background: transparent; color: #c4956a;
              text-decoration: none; border-radius: 6px; border: 1px solid #c4956a;
              font-family: 'Outfit', -apple-system, sans-serif; font-size: 14px; font-weight: 400;
              letter-spacing: 0.04em;">
      Sign in to Loore
    </a>
    <div style="border-top: 1px solid #302c27; margin-top: 36px; padding-top: 20px;">
      <p style="color: #736b5f; font-size: 13px; font-weight: 300; margin: 0 0 6px 0; line-height: 1.5;">
        This link expires in 15 minutes and can only be used once.
      </p>
      <p style="color: #736b5f; font-size: 12px; font-weight: 300; margin: 0; line-height: 1.5;">
        If you didn't request this, you can safely ignore this email.
      </p>
    </div>
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


def send_welcome_email(to_email, magic_link_url):
    config = current_app.config
    sender = config.get("MAIL_DEFAULT_SENDER", "login@loore.org")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Welcome to Loore"
    msg["From"] = sender
    msg["To"] = to_email

    text_body = (
        "Welcome to Loore\n\n"
        "Your account has been approved! You're one of the first people here.\n\n"
        f"Click the link below to get started:\n{magic_link_url}\n\n"
        "This link expires in 15 minutes and can only be used once.\n\n"
        "There's no wrong way to do this. Write about today, talk about a dream, "
        "process something that's been sitting in you. Loore will meet you wherever you are."
    )

    html_body = f"""\
<html>
<body style="font-family: 'Outfit', -apple-system, sans-serif; background: #0e0d0b; color: #ede8dd; padding: 40px 20px; margin: 0;">
  <div style="max-width: 460px; margin: 0 auto; background: #181714; border-radius: 10px; border: 1px solid #302c27; padding: 48px 40px;">
    <div style="font-family: 'Cormorant Garamond', Georgia, serif; font-size: 14px; font-weight: 300; text-transform: uppercase; letter-spacing: 0.3em; color: #736b5f; margin-bottom: 32px;">
      Loore
    </div>
    <div style="font-size: 28px; color: #c4956a; opacity: 0.5; margin-bottom: 16px;">&#10022;</div>
    <h2 style="font-family: 'Cormorant Garamond', Georgia, serif; font-weight: 300; font-size: 28px; color: #ede8dd; margin: 0 0 12px 0;">
      Welcome to <em style="color: #c4956a;">Loore</em>.
    </h2>
    <p style="font-size: 15px; font-weight: 300; color: #a89f91; margin: 0 0 8px 0; line-height: 1.6;">
      Your account has been approved. You're one of the first people here.
    </p>
    <p style="font-size: 15px; font-weight: 300; color: #a89f91; margin: 0 0 28px 0; line-height: 1.6;">
      Click below to begin.
    </p>
    <a href="{magic_link_url}"
       style="display: inline-block; padding: 12px 32px; background: transparent; color: #c4956a;
              text-decoration: none; border-radius: 6px; border: 1px solid #c4956a;
              font-family: 'Outfit', -apple-system, sans-serif; font-size: 14px; font-weight: 400;
              letter-spacing: 0.04em;">
      Enter Loore
    </a>
    <div style="border-top: 1px solid #302c27; margin-top: 36px; padding-top: 20px;">
      <p style="font-family: 'Cormorant Garamond', Georgia, serif; font-weight: 300; font-style: italic; font-size: 15px; color: #a89f91; margin: 0 0 12px 0; line-height: 1.5;">
        There's no wrong way to do this.
      </p>
      <p style="color: #736b5f; font-size: 13px; font-weight: 300; margin: 0 0 6px 0; line-height: 1.5;">
        This link expires in 15 minutes and can only be used once.
      </p>
    </div>
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
        logger.info(f"Welcome email sent to {to_email}")
    except Exception:
        logger.exception(f"Failed to send welcome email to {to_email}")
        raise
