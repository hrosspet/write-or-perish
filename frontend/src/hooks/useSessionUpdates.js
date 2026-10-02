import { useCallback, useEffect, useRef, useState } from 'react';
import api from '../api';

// The page a newly activated user lands on (the approval email links
// here). The updates modal never opens over it (#392).
export const WELCOME_PATH = '/welcome';

/**
 * The dev-update channel (#207): once per session, after terms are
 * settled, ask what's unread. Returns [updates, clearUpdates]; updates
 * stays null when nothing is unread, and then no modal is shown.
 *
 * A session that starts on /welcome doesn't ask at all (#392): not on
 * /welcome, and not on the next page either, since that is where
 * "Reflect" takes a newcomer to write their first entry. Nothing is lost:
 * whatever is unread is shown on the next visit.
 */
export function useSessionUpdates(user, pathname) {
  const [updates, setUpdates] = useState(null);
  const asked = useRef(false);

  useEffect(() => {
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
