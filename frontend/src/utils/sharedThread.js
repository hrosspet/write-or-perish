// A thread other people can read (Peter, 2026-11-12): the node a reply
// would answer, or a new entry, is anything but private (public, or
// shared with circles). Loore never replies by itself there: auto-generate
// is off and can't be turned on, and every AI reply is created private
// (the server's rule, backend/utils/llm_nodes.py shared_with_others).
// A sharing that doesn't show in the node's own privacy has to be added
// here and in shared_with_others.
export const isSharedPrivacy = (privacyLevel) =>
  !!privacyLevel && privacyLevel !== 'private';

export const AUTO_GENERATE_OFF_CODE = 'auto_generate_off';
export const AUTO_GENERATE_OFF_HINT = 'Auto-generate is off in public threads.';

// The server's refusal of an automatic reply under a shared node (409).
export const isAutoGenerateOff = (err) =>
  err?.response?.status === 409
  && err?.response?.data?.code === AUTO_GENERATE_OFF_CODE;

// The server's answer when something about to be public quotes an entry
// that isn't (backend/utils/publish_quotes.py): 409 with the quoted
// entries. Returns { message, quoted } or null.
export const QUOTES_NOT_PUBLIC_CODE = 'quotes_not_public';
export const quotesNotPublicError = (err) => {
  const data = err?.response?.data;
  if (err?.response?.status !== 409 || data?.code !== QUOTES_NOT_PUBLIC_CODE) {
    return null;
  }
  return { message: data.error, quoted: Array.isArray(data.quoted) ? data.quoted : [] };
};
