// Every /dashboard/email endpoint answers with the account's email state
// (#260). Merging it into the user context keeps each page showing what the
// server has — including a change that was confirmed in another tab or on
// another device since this page loaded.
export function emailState(data) {
  return {
    email: data.email ?? null,
    pending_email: data.pending_email ?? null,
    pending_email_expired: Boolean(data.pending_email_expired),
  };
}
