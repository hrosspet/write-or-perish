import { useCallback, useEffect, useRef, useState } from 'react';
import { WELCOME_PATH, QUIET_PATHS } from './useSessionUpdates';
import { isRecordingActive, subscribeRecording } from '../utils/recordingActivity';

// Set for the tab when the user closes the both-providers question
// without answering (#599): no page asks again on opening Loore until the
// next visit (a new tab). An import's result and a preferred-model change
// still ask, as the rules say.
export const LATER_FLAG = 'loore.bothProvidersLater';

// Pages where the question never opens on its own: a Voice or Text
// session, the newcomer's welcome, the Account page (its own setting, and
// its own question after a preferred-model change), the Import page (its
// result asks), and the signed-out and account-deletion pages.
export const NO_ASK_PATHS = [
  '/voice', '/textmode', WELCOME_PATH, '/account', '/import',
  '/alpha-thank-you', '/landing', '/login', '/confirm-email',
  '/account-deleted', ...QUIET_PATHS,
];

const storage = (fn) => { try { return fn(window.sessionStorage); } catch (e) { return null; } };

export function noteBothProvidersLater() {
  storage((s) => s.setItem(LATER_FLAG, '1'));
}

/**
 * The question on opening Loore (#599): once per visit, when the server
 * says to ask (user.ask_both_providers.visit: never answered, at least one
 * entry), on the first page that suits it. Never while a recording is in
 * progress, on NO_ASK_PATHS, or while another dialog is open (``blocked``):
 * it waits for the next page or for the recording or dialog to end. A
 * visit that starts on /welcome or a quiet page does not ask, as for the
 * updates modal (#392).
 *
 * Returns [open, close]; close(answered) records a close without an
 * answer for the rest of the visit.
 */
export function useBothProvidersQuestion(user, pathname, blocked) {
  const [open, setOpen] = useState(false);
  const shown = useRef(false);
  const skipVisit = useRef(null);
  const openedAt = useRef(null);
  const [recording, setRecording] = useState(isRecordingActive());

  useEffect(() => subscribeRecording(setRecording), []);

  useEffect(() => {
    if (skipVisit.current === null) {
      skipVisit.current = pathname === WELCOME_PATH || QUIET_PATHS.includes(pathname);
    }
    if (shown.current || skipVisit.current) return;
    if (!user || !user.approved || !user.terms_up_to_date) return;
    if (!user.ask_both_providers || !user.ask_both_providers.visit) return;
    if (storage((s) => s.getItem(LATER_FLAG))) return;
    if (NO_ASK_PATHS.includes(pathname)) return;
    if (blocked || recording || isRecordingActive()) return;
    shown.current = true;
    openedAt.current = pathname;
    setOpen(true);
  }, [user, pathname, blocked, recording]);

  const close = useCallback((answered) => {
    if (!answered) noteBothProvidersLater();
    setOpen(false);
  }, []);

  // Leaving the page (for example by the link to the Account page) closes
  // it; without an answer, that counts as "later".
  useEffect(() => {
    if (open && openedAt.current !== null && pathname !== openedAt.current) {
      close(false);
    }
  }, [open, pathname, close]);

  return [open, close];
}
