// Payments (#438), on the web. Everything here is inert unless the server
// has payments on: the user object then carries `payments_enabled` and
// `subscription`, and an account without AI replies gets a 402
// `subscription_required` (or, where its writing was saved, a 2xx with
// `subscription_required: true`) instead of a reply.
//
// Like utils/spendCap.js, a session flag lets Voice refuse a recording
// before it starts, and an event ('loore:subscription-required') raises
// SubscriptionBanner, whose link goes to Settings. The iPhone app shows
// the same server message without the link.

const EVENT = 'loore:subscription-required';

let _paymentsEnabled = false;
let _required = false;
let _message = null;

if (typeof window !== 'undefined') {
  window.addEventListener(EVENT, (e) => {
    _required = true;
    if (e && e.detail && e.detail.message) _message = e.detail.message;
  });
}

export const DEFAULT_SUBSCRIPTION_MESSAGE =
  "Loore doesn't reply on an account without a subscription. " +
  'Your writing is saved.';

// Called with the user object on every load (UserContext).
// The server's state replaces what the session learned (a subscription
// that just started clears the flag).
export function rememberSubscription(user) {
  _paymentsEnabled = !!(user && user.payments_enabled);
  const sub = user && user.subscription;
  _required = !!(_paymentsEnabled && sub && sub.ai_replies === false);
}

// Payments on and the account was never let in (no subscription yet, or
// its first payment failed): the app shows only the subscribe page.
export function notLetIn(user) {
  return !!(user && user.approved && user.payments_enabled
    && user.subscription && user.subscription.state === 'none');
}

export function paymentsEnabled() {
  return _paymentsEnabled;
}

export function isSubscriptionRequired() {
  return _required;
}

export function subscriptionMessage() {
  return _message || DEFAULT_SUBSCRIPTION_MESSAGE;
}

// Raise the banner (and set the flag): a refused press, a 402, or a 2xx
// that saved the writing and skipped the reply.
export function notifySubscriptionRequired(message) {
  _required = true;
  if (message) _message = message;
  try {
    window.dispatchEvent(new CustomEvent(EVENT, {
      detail: { message: subscriptionMessage() },
    }));
  } catch (e) {
    // Ignore — CustomEvent unavailable.
  }
}

// For an answer that saved the writing: raise the banner when it says the
// reply was skipped for the subscription.
export function noteSkippedReply(data) {
  if (data && data.subscription_required) {
    notifySubscriptionRequired(data.subscription_message);
  }
}

export function isSubscriptionRequiredError(err) {
  const res = err && err.response;
  return !!res && res.status === 402
    && !!res.data && res.data.error === 'subscription_required';
}

export const SUBSCRIPTION_REQUIRED_EVENT = EVENT;

// Tests only.
export function resetSubscriptionForTests() {
  _paymentsEnabled = false;
  _required = false;
  _message = null;
}

// ── Display ──────────────────────────────────────────────────────────

export const LEVEL_NAMES = {
  standard: 'Standard',
  supporter: 'Supporter',
  discounted: 'Reduced',
};

export function formatPrice(cents, currency = 'usd') {
  const amount = (cents || 0) / 100;
  try {
    return new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency: currency.toUpperCase(),
      minimumFractionDigits: amount % 1 ? 2 : 0,
    }).format(amount);
  } catch (e) {
    return `${amount} ${currency.toUpperCase()}`;
  }
}

export function formatDay(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString(undefined, {
    year: 'numeric', month: 'long', day: 'numeric', timeZone: 'UTC',
  });
}

// One line on the state: what the account has and the next date.
export function subscriptionStatusLine(sub) {
  if (!sub) return null;
  const level = LEVEL_NAMES[sub.level] || null;
  const end = formatDay(sub.current_period_end);
  const trialEnd = formatDay(sub.trial_end);
  switch (sub.status) {
    case 'trialing':
      // An alpha tester who subscribed during the free month: the trial is
      // the rest of that month.
      if (sub.alpha_tester) {
        if (sub.cancel_at_period_end) {
          return `${level}, cancelled. Your free month runs until ${trialEnd || end}; you won't be charged.`;
        }
        return `${level}. Your free month runs until ${trialEnd || end}; the first payment is on that day.`;
      }
      if (sub.cancel_at_period_end) {
        return `${level} trial, cancelled. It ends on ${trialEnd || end}; you won't be charged.`;
      }
      return `${level} trial until ${trialEnd || end}. The first payment is on that day.`;
    case 'active':
      if (sub.cancel_at_period_end) {
        return `${level}, cancelled. It ends on ${end}.`;
      }
      return `${level}. Next payment on ${end}.`;
    case 'past_due':
      if (sub.state === 'none') {
        // The first payment failed: the account was never let in.
        return `${level}. The first payment didn't go through, so Loore stays closed until it does. Update your card to continue.`;
      }
      return `${level}. The last payment didn't go through, so AI replies, Voice mode and profile updates are paused until it does. Update your card to continue.`;
    case 'ended':
      // Ended before a first payment: the subscribe page says the account
      // has no active subscription; nothing about writing that stays.
      if (sub.state === 'none') return null;
      return 'Your subscription has ended. Your writing, reading and export stay; AI replies, Voice mode and profile updates are paused.';
    default:
      return null;
  }
}

// ── The alpha free month (#438) ──────────────────────────────────────
// Alpha testers have a free month from the Beta release date, the same
// dates for everyone (`sub.free_month_start`, `sub.free_month_until`), then
// the discounted price without a code. Before it (`free_month_upcoming`)
// nothing changes for them; `free_month_active` while it runs.

// Days before the end of the free month when the web says it ends soon.
export const FREE_MONTH_NOTICE_DAYS = 7;

// The free month's line in Settings, for a tester without a live
// subscription. `price` is the discounted level's, formatted.
export function freeMonthLine(sub, price) {
  if (!sub || !sub.alpha_tester) return null;
  const until = formatDay(sub.free_month_until);
  if (sub.free_month_upcoming) {
    const after = price ? ` After it, Loore is ${price} a month for you, plus tax where it applies.` : '';
    return `Your free month as an alpha tester runs from ${formatDay(sub.free_month_start)} to ${until}. Until it starts, nothing changes.${after}`;
  }
  if (sub.free_month_active) {
    const after = price ? ` After it, Loore is ${price} a month for you, plus tax where it applies.` : '';
    return `Your free month as an alpha tester runs until ${until}.${after}`;
  }
  return 'Your free month has ended. Your writing, reading and export stay; AI replies, Voice mode and profile updates are paused.';
}

// Whether the web should say that the free month ends soon: a tester
// without a live subscription in the month's last FREE_MONTH_NOTICE_DAYS.
export function freeMonthEndsSoon(sub, now = Date.now()) {
  if (!sub || !sub.alpha_tester || !sub.free_month_active) return false;
  if (['trialing', 'active', 'past_due'].includes(sub.status)) return false;
  const end = sub.free_month_until ? new Date(sub.free_month_until).getTime() : NaN;
  if (Number.isNaN(end)) return false;
  return end - now <= FREE_MONTH_NOTICE_DAYS * 86400000;
}
