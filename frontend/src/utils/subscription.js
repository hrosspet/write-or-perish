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
      return `${level}. The last payment didn't go through; Stripe will try again. Update your card to keep it.`;
    case 'ended':
      return 'Your subscription has ended. Your writing, reading and export stay; AI replies, Voice mode and profile updates are paused.';
    default:
      return null;
  }
}
