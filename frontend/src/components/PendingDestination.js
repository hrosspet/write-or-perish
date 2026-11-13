import { useEffect, useLayoutEffect, useRef } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useUser } from '../contexts/UserContext';
import { here, rememberArrival, takePending } from '../utils/destination';

// After signing in, back to the page where the sign-in started (#704), also
// when the account's steps took the person away from it (utils/destination).

// In App, above anything that shows a step page instead of the app: the page
// a signed-in account arrived on in this page load. A layout effect, so it is
// kept before <PendingDestination /> looks in the same commit.
export function useRememberArrival() {
  const { user, loading } = useUser();
  const location = useLocation();
  // The address the page loaded at, before any redirect of the app's.
  const arrival = useRef(here(location));
  const done = useRef(false);
  useLayoutEffect(() => {
    if (loading || done.current) return;
    done.current = true;
    if (user) rememberArrival(arrival.current);
  }, [user, loading]);
}

// Mounted with the app itself (not with a step page): the arrival is taken
// at once. One from an earlier page load is opened; one from this load is
// where the person is already. Signed out, or waiting for approval: dropped.
export default function PendingDestination() {
  const { user, loading } = useUser();
  const navigate = useNavigate();
  useEffect(() => {
    if (loading) return;
    const pending = takePending();
    if (!pending || !user || !user.approved || !pending.earlierLoad) return;
    navigate(pending.path, { replace: true });
  }, [user, loading, navigate]);
  return null;
}
