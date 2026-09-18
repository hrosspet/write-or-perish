import re
import smtplib
import logging
from html import escape
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import current_app

logger = logging.getLogger(__name__)

# The one definition of "an address we accept", for sign-in and for the
# email change alike. The length is the width of User.email: a longer
# address passes the pattern and then fails the insert.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EMAIL_MAX_LENGTH = 128


def is_valid_email(address):
    return bool(address) and len(address) <= EMAIL_MAX_LENGTH \
        and bool(EMAIL_RE.match(address))


def _deliver(to_email, subject, text_body, html_body):
    """Send one multipart mail through the configured SMTP relay."""
    config = current_app.config
    sender = config.get("MAIL_DEFAULT_SENDER", "login@loore.org")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_email
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    server = config.get("MAIL_SERVER", "localhost")
    port = config.get("MAIL_PORT", 587)
    use_tls = config.get("MAIL_USE_TLS", True)
    username = config.get("MAIL_USERNAME")
    password = config.get("MAIL_PASSWORD")
    timeout = config.get("MAIL_TIMEOUT_SECONDS", 10)
    with smtplib.SMTP(server, port, timeout=timeout) as smtp:
        if use_tls:
            smtp.starttls()
        if username and password:
            smtp.login(username, password)
        smtp.sendmail(sender, to_email, msg.as_string())


def _send(to_email, subject, text_body, html_body, sent_log, failed_log,
          best_effort=False):
    """_deliver plus the sender's log lines. ``best_effort`` mails (admin
    heads-ups, notices about something that already happened) never raise."""
    try:
        _deliver(to_email, subject, text_body, html_body)
        logger.info(sent_log)
    except Exception:
        logger.exception(failed_log)
        if not best_effort:
            raise


_P_STYLE = "font-size: 15px; font-weight: 300; color: #a89f91; margin: 0 0 {gap} 0; line-height: 1.6;"
_NOTE_STYLES = (
    "color: #736b5f; font-size: 13px; font-weight: 300; margin: 0 0 6px 0; line-height: 1.5;",
    "color: #736b5f; font-size: 12px; font-weight: 300; margin: 0; line-height: 1.5;",
)


def _card(heading, paragraphs, button_label=None, button_url=None, footnotes=()):
    """The dark Loore mail card every mail uses. ``heading``, ``paragraphs``
    and ``footnotes`` are HTML: escape anything a user typed."""
    # The wider gap below the last paragraph is room for the button.
    body = "".join(
        f'\n    <p style="{_P_STYLE.format(gap="28px" if button_url and i == len(paragraphs) - 1 else "8px")}">'
        f"\n      {p}\n    </p>"
        for i, p in enumerate(paragraphs))
    button = ""
    if button_url:
        button = f"""
    <a href="{button_url}"
       style="display: inline-block; padding: 12px 32px; background: transparent; color: #c4956a;
              text-decoration: none; border-radius: 6px; border: 1px solid #c4956a;
              font-family: 'Outfit', -apple-system, sans-serif; font-size: 14px; font-weight: 400;
              letter-spacing: 0.04em;">
      {button_label}
    </a>"""
    footer = ""
    if footnotes:
        notes = "".join(
            f'\n      <p style="{_NOTE_STYLES[min(i, 1)]}">\n        {n}\n      </p>'
            for i, n in enumerate(footnotes))
        footer = f"""
    <div style="border-top: 1px solid #302c27; margin-top: 36px; padding-top: 20px;">{notes}
    </div>"""
    return f"""\
<html>
<body style="font-family: 'Outfit', -apple-system, sans-serif; background: #0e0d0b; color: #ede8dd; padding: 40px 20px; margin: 0;">
  <div style="max-width: 460px; margin: 0 auto; background: #181714; border-radius: 10px; border: 1px solid #302c27; padding: 48px 40px;">
    <div style="font-family: 'Cormorant Garamond', Georgia, 'Times New Roman', serif; font-size: 14px; font-weight: 300; text-transform: uppercase; letter-spacing: 0.3em; color: #736b5f; margin-bottom: 32px;">
      Loore
    </div>
    <h2 style="font-family: 'Cormorant Garamond', Georgia, 'Times New Roman', serif; font-weight: 300; font-size: 28px; color: #ede8dd; margin: 0 0 12px 0;">
      {heading}
    </h2>{body}{button}{footer}
  </div>
</body>
</html>"""


def _duration_words(seconds):
    """900 -> "15 minutes", 86400 -> "24 hours": the mail states the
    configured lifetime instead of repeating a number that can drift."""
    seconds = int(seconds)
    for unit, size in (("hour", 3600), ("minute", 60)):
        if seconds >= size and seconds % size == 0:
            n = seconds // size
            return f"{n} {unit}{'' if n == 1 else 's'}"
    return f"{seconds} seconds"


def send_magic_link_email(to_email, magic_link_url):
    lifetime = _duration_words(
        current_app.config.get("MAGIC_LINK_EXPIRY_SECONDS", 900))
    text_body = (
        "Sign in to Loore\n\n"
        f"Click the link below to sign in:\n{magic_link_url}\n\n"
        f"This link expires in {lifetime} and can only be used once.\n\n"
        "If you didn't request this, you can safely ignore this email."
    )
    html_body = _card(
        "Sign in",
        ["Click the button below to continue to your account."],
        "Sign in to Loore", magic_link_url,
        (f"This link expires in {lifetime} and can only be used once.",
         "If you didn't request this, you can safely ignore this email."))
    _send(to_email, "Your Loore sign-in link", text_body, html_body,
          f"Magic link email sent to {to_email}",
          f"Failed to send magic link email to {to_email}")


def send_email_change_email(to_email, confirm_url, expires_in_seconds):
    """Confirmation link for binding *to_email* to an account (#260). The
    address becomes the account's email only when the link is confirmed
    from inside that account (POST /api/dashboard/email/confirm)."""
    lifetime = _duration_words(expires_in_seconds)
    text_body = (
        "Confirm your email for Loore\n\n"
        "Someone asked to use this address for their Loore account. "
        f"Open the link below to confirm it:\n{confirm_url}\n\n"
        f"This link expires in {lifetime}.\n\n"
        "If this wasn't you, ignore this email and nothing changes."
    )
    html_body = _card(
        "Confirm your email",
        ["Someone asked to use this address for their Loore account. "
         "Confirm it to make it your sign-in email."],
        "Confirm this address", confirm_url,
        (f"This link expires in {lifetime}.",
         "If this wasn't you, ignore this email and nothing changes."))
    _send(to_email, "Confirm your email for Loore", text_body, html_body,
          "Email-change confirmation sent",
          "Failed to send email-change confirmation")


def send_email_in_use_notice(to_email):
    """Sent INSTEAD of the confirmation link when the requested address
    already signs in to another account (#260). The requester gets the same
    response either way, so the endpoint cannot be used to find out which
    addresses have accounts; only whoever reads this inbox learns why no
    link came."""
    text_body = (
        "This address already signs in to Loore\n\n"
        "Someone asked to use this address for a Loore account, but it "
        "already signs in to another one. Nothing has changed.\n\n"
        "If this was you: sign in with this address to reach the account it "
        "belongs to, or use a different address for the other account.\n\n"
        "If it wasn't you, you can ignore this email."
    )
    html_body = _card(
        "This address is already in use",
        ["Someone asked to use this address for a Loore account, but it "
         "already signs in to another one. Nothing has changed."],
        footnotes=(
            "If this was you: sign in with this address to reach the account "
            "it belongs to, or use a different address for the other account.",
            "If it wasn't you, you can ignore this email."))
    _send(to_email, "This address already signs in to Loore", text_body,
          html_body,
          "Email-in-use notice sent",
          "Failed to send email-in-use notice")


def send_email_changed_notice(old_email, new_email):
    """Tell the previous address that the account moved to a new one, so a
    hijacked session cannot silently re-home the account (#260). The change
    itself already happened; the notice is best-effort."""
    text_body = (
        "Your Loore sign-in email changed\n\n"
        f"The email for your Loore account is now {new_email}. "
        "This address no longer signs you in.\n\n"
        "If you did not do this, reply to this email right away."
    )
    html_body = _card(
        "Your sign-in email changed",
        [f"The email for your Loore account is now <strong style=\"color: #ede8dd;\">{escape(new_email)}</strong>. "
         "This address no longer signs you in."],
        footnotes=("If you did not do this, reply to this email right away.",))
    _send(old_email, "Your Loore sign-in email changed", text_body, html_body,
          "Email-changed notice sent to the previous address",
          "Failed to send email-changed notice",
          best_effort=True)


def send_welcome_email(to_email, magic_link_url):
    text_body = (
        "Welcome to Loore\n\n"
        "Your account has been approved! You're one of the first people here.\n\n"
        f"Click the link below to get started:\n{magic_link_url}\n\n"
        "This link can only be used once."
    )
    html_body = _card(
        'Welcome to <em style="color: #c4956a;">Loore</em>.',
        ["Your account has been approved. You're one of the first people here.",
         "Click below to begin."],
        "Enter Loore", magic_link_url,
        ("This link can only be used once.",))
    _send(to_email, "Welcome to Loore", text_body, html_body,
          f"Welcome email sent to {to_email}",
          f"Failed to send welcome email to {to_email}")


def send_spend_alert_email(to_email, provider, spend_usd, limit_usd, threshold):
    """Alert the admin that month-to-date API spend crossed a threshold
    of the configured limit (issue #85)."""
    percent = int(round(threshold * 100))
    text_body = (
        f"API spend alert\n\n"
        f"Provider: {provider}\n"
        f"Month-to-date spend: ${spend_usd:,.2f}\n"
        f"Monthly limit: ${limit_usd:,.2f}\n"
        f"Threshold crossed: {percent}%\n\n"
        f"Review usage in the admin dashboard: https://loore.org/admin\n"
    )
    html_body = _card(
        f"Spend alert: {percent}%",
        [f'<strong style="color: #ede8dd;">{escape(str(provider))}</strong> month-to-date spend is\n'
         f'      <strong style="color: #c4956a;">${spend_usd:,.2f}</strong>\n'
         f'      of the <strong style="color: #ede8dd;">${limit_usd:,.2f}</strong> monthly limit.'],
        "Open admin dashboard", "https://loore.org/admin")
    _send(to_email,
          f"Loore spend alert: {provider} at {percent}% of monthly limit",
          text_body, html_body,
          f"Spend alert email sent to {to_email} ({percent}%)",
          f"Failed to send spend alert email to {to_email}")


def send_user_spend_block_email(to_email, username, spend_usd, limit_usd):
    """Alert the admin that a single user hit their monthly spend cap and has
    been hard-blocked until month rollover (issue #85 follow-up)."""
    text_body = (
        f"Per-user spend cap reached\n\n"
        f"User: {username}\n"
        f"Month-to-date spend: ${spend_usd:,.2f}\n"
        f"Monthly cap: ${limit_usd:,.2f}\n\n"
        f"This user is now hard-blocked from cost-incurring actions until the "
        f"month rolls over.\n"
        f"Review usage in the admin dashboard: https://loore.org/admin\n"
    )
    html_body = _card(
        "User hit monthly cap",
        [f'<strong style="color: #ede8dd;">{escape(str(username))}</strong> reached\n'
         f'      <strong style="color: #c4956a;">${spend_usd:,.2f}</strong>\n'
         f'      of the <strong style="color: #ede8dd;">${limit_usd:,.2f}</strong> monthly cap\n'
         f'      and is now hard-blocked until the month rolls over.'],
        "Open admin dashboard", "https://loore.org/admin")
    _send(to_email,
          f"Loore: user {username} hit the ${limit_usd:,.0f} monthly cap",
          text_body, html_body,
          f"Spend block email sent to {to_email} (user {username})",
          f"Failed to send spend block email to {to_email}")


def send_admin_signup_notification(username, user_email):
    admin_email = "signup@loore.org"
    admin_url = "https://loore.org/admin"
    text_body = (
        f"New signup on Loore\n\n"
        f"Username: {username}\n"
        f"Email: {user_email or 'not provided'}\n\n"
        f"Review and approve in the admin dashboard:\n{admin_url}\n"
    )
    shown_email = escape(user_email) if user_email else "<em>not provided</em>"
    html_body = _card(
        "New signup",
        [f'<strong style="color: #ede8dd;">{escape(str(username))}</strong> just accepted the terms.',
         f"Email: {shown_email}"],
        "Open admin dashboard", admin_url)
    _send(admin_email, f"New signup: {username}", text_body, html_body,
          f"Admin signup notification sent for user {username}",
          f"Failed to send admin signup notification for user {username}",
          best_effort=True)


def send_admin_prefill_complete_notification(username, handle, versions,
                                             source_tokens, approved):
    """Admin heads-up that a pre-filled account's INITIAL profile chain has
    finished (single chunk, or chunks + integration) — the whole build, not
    just the first version. Sent once per account; routine updates of
    active users never trigger it (see profile_batch._notify_prefill_complete)."""
    admin_email = "signup@loore.org"
    admin_url = "https://loore.org/admin"
    status = "Active" if approved else "Inactive — not yet activated"
    tokens = f"{source_tokens:,}" if source_tokens else "?"
    text_body = (
        f"Pre-fill profile ready on Loore\n\n"
        f"Username: {username}\n"
        f"Seeded from: @{handle}\n"
        f"Chain: {versions} version(s), {tokens} source tokens\n"
        f"Account: {status}\n\n"
        f"Admin dashboard:\n{admin_url}\n"
    )
    html_body = _card(
        "Pre-fill profile ready",
        [f'<strong style="color: #ede8dd;">{escape(str(username))}</strong>\'s initial profile chain finished\n'
         f'      (seeded from <strong style="color: #ede8dd;">@{escape(str(handle))}</strong>).',
         f"{versions} version(s) &middot; {tokens} source tokens",
         f"Account: {status}"],
        "Open admin dashboard", admin_url)
    _send(admin_email, f"Pre-fill profile ready: {username}", text_body,
          html_body,
          f"Admin pre-fill-complete notification sent for user {username}",
          f"Failed to send admin pre-fill-complete notification for user {username}",
          best_effort=True)
