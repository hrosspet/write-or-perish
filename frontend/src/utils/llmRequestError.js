// A request for an AI reply (POST /nodes/<id>/llm) that failed or was
// refused, after LLM Response or after an entry with Auto-generate on.
// Every path that makes the request shows the failure the same way
// (#747): a toast with the server's sentence, unless the page already
// says it another way. The iPhone app's copy is LLMRequestError.

import { isAutoGenerateOff } from './sharedThread';
import { isThreadTooLong } from './threadLength';

export const LLM_REQUEST_FAILED_TEXT = 'Error requesting LLM response.';

// The toast for *err*, or null when there is none to show:
// - 402 (the monthly usage limit, or no subscription): the banner says it
//   (the api.js interceptor raises it);
// - an automatic reply refused under a shared node: not an error, LLM
//   Response stays offered;
// - a thread too long for another reply (#648): the reply form shows the
//   limit, on the page the refusal came from or on the entry's page.
export const llmRequestErrorToast = (err) => {
  if (err?.response?.status === 402) return null;
  if (isAutoGenerateOff(err) || isThreadTooLong(err)) return null;
  return err?.response?.data?.error || err?.message || LLM_REQUEST_FAILED_TEXT;
};
