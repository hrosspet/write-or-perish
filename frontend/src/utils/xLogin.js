// Where Connect X (#311) comes back to: /account?x_login=<outcome>#x. An
// account without a subscription sees the subscribe page at that address,
// so both pages read the outcome.
export const X_LOGIN_MESSAGES = {
  linked: { type: "success", text: "X connected. Sign in with X now opens this account." },
  cancelled: { type: "info", text: "X was not connected." },
  failed: { type: "error", text: "Could not read your X account. Please try again." },
  // Connect X was started many times from this network (backend/oauth.py).
  wait: { type: "error", text: "Connect X was started many times from this network. Please wait a few minutes, then try again." },
  other_x: { type: "error", text: "This account is already connected to a different X account." },
  taken: {
    type: "error",
    text: "That X account already signs in to another Loore account, so it can't be connected here. If that account was made by accident, write to info@loore.org and we'll remove it so you can connect X here.",
  },
  // An account set up for that X account and never signed in to, which is
  // being deleted or was blocked by the admin. One that is neither is
  // offered at /prepared-account instead (#714).
  taken_placeholder: {
    type: "error",
    text: "An account was set up for that X account, but it can't be opened now. Write to info@loore.org.",
  },
};

// The subscribe page starts Connect X from "Did someone prepare a Loore
// account for you?" (#714), so two outcomes answer that question.
export const SUBSCRIBE_X_LOGIN_MESSAGES = {
  ...X_LOGIN_MESSAGES,
  linked: {
    type: "success",
    text: "X connected. No other Loore account was prepared for that X account, so Sign in with X now opens this one.",
  },
  taken: {
    type: "info",
    text: "That X account already signs in to another Loore account. To open it, sign out and use Sign in with X.",
  },
};

export function xLoginOutcome(search) {
  return new URLSearchParams(search).get("x_login");
}
