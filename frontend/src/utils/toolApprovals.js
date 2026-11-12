// The AI's calls that change the user's data wait for their approval when
// the thread holds text they didn't write (Peter, 2026-11-12). In Voice
// mode no new turn starts in that thread until each is decided: the server
// refuses one with 409 {"error", "code": "approval_pending", "pending": n}
// (backend/utils/tool_approvals.py).
export const APPROVAL_PENDING_CODE = 'approval_pending';

/** True for the server's refusal of a Voice turn while a change waits. */
export function isApprovalPendingError(err) {
  const data = err && err.response && err.response.data;
  return !!data && data.code === APPROVAL_PENDING_CODE;
}

/** The Voice screen's line by the disabled record button. */
export function voiceWaitingLine(count) {
  return count === 1
    ? 'A change to your data waits for your approval'
    : `${count} changes to your data wait for your approval`;
}
