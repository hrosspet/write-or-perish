import { useCallback, useEffect, useRef, useState } from 'react';
import api from '../api';

// The page a newly activated user lands on (the approval email links
// here). The updates modal never opens over it (#392).
export const WELCOME_PATH = '/welcome';

// The account deletion confirmation (emailed link) and the restore offer.
// The updates modal never opens over them either, nor over the page after:
// a restore signs in with a full page load, so the next page is a new
// session and only this flag, kept for the tab, carries the decision over.
export const QUIET_PATHS = ['/confirm-account-deletion', '/account-restore'];
export const QUIET_FLAG = 'loore_no_updates_once';

const storage = (fn) => { try { return fn(window.sessionStorage); } catch (e) { return null; } };

/**
 * The dev-update channel (#207): once per session, after terms are
 * settled, ask what's unread. Returns [updates, clearUpdates]; updates
 * stays null when nothing is unread, and then no modal is shown.
 *
 * A session that starts on /welcome doesn't ask at all (#392): not on
 * /welcome, and not on the next page either, since that is where
 * "Reflect" takes a newcomer to write their first entry. Nothing is lost:
 * whatever is unread is shown on the next visit.
 *
 * The same holds for a session that starts on one of QUIET_PATHS (account
 * deletion confirmation, account restore): no ask there, and none on the
 * page it leads to, even after a full reload (restore signs in that way).
 */
export function useSessionUpdates(user, pathname) {
  const [updates, setUpdates] = useState(null);
  const asked = useRef(false);
  const started = useRef(false);

  useEffect(() => {
    if (!started.current) {
      started.current = true;
      if (QUIET_PATHS.includes(pathname)) {
        asked.current = true;
        storage((s) => s.setItem(QUIET_FLAG, '1'));
      } else if (storage((s) => s.getItem(QUIET_FLAG))) {
        // The page after a quiet one: consume the flag, stay silent.
        asked.current = true;
        storage((s) => s.removeItem(QUIET_FLAG));
      }
    }
    if (!user || !user.approved || !user.terms_up_to_date) return;
    if (asked.current) return;
    asked.current = true;
    if (pathname === WELCOME_PATH) return;
    api.get('/updates').then((res) => {
      const d = res.data || {};
      const count = (d.changelog?.length || 0) +
        (d.notifications?.length || 0) + (d.polls?.length || 0);
      if (count > 0) setUpdates(d);
    }).catch(() => {});
  }, [user, pathname]);

  const clearUpdates = useCallback(() => setUpdates(null), []);
  return [updates, clearUpdates];
}
