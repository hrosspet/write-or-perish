import { useEffect, useLayoutEffect, useRef } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useUser } from '../contexts/UserContext';
import {
  forgetPending, here, isAt, isCheckoutReturn, peekPending, rememberArrival,
} from '../utils/destination';

// After signing in, back to the page where the sign-in started (#704), also
// when the account's steps took the person away from it (utils/destination).

// How often the kept page is opened (heuristic, #704): once more when
// another navigation of the same moment replaced the first, and never a
// third time, so two pages that each send the person on cannot loop.
export const OPEN_TRIES = 2;

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

// Mounted with the app itself (not with a step page). An arrival from an
// earlier page load is opened, and forgotten once the person is there; one
// from this load is where the person is already. Signed out, or waiting
// for approval: dropped.
export default function PendingDestination() {
  const { user, loading } = useUser();
  const navigate = useNavigate();
  const location = useLocation();
  const current = here(location);
  // Back from a checkout, the Account page first drops the checkout's
  // query, a navigation of its own in the same moment that would replace
  // this one: wait until it has.
  const checkoutReturn = isCheckoutReturn(location.search);
  const tries = useRef(0);
  useEffect(() => {
    if (loading || checkoutReturn) return;
    const pending = peekPending();
    if (!pending) return;
    if (!user || !user.approved || !pending.earlierLoad || isAt(current, pending.path)) {
      forgetPending();
      return;
    }
    tries.current += 1;
    // The last try forgets it as it goes: nothing is left to open later.
    if (tries.current >= OPEN_TRIES) forgetPending();
    navigate(pending.path, { replace: true });
  }, [user, loading, navigate, checkoutReturn, current]);
  return null;
}
