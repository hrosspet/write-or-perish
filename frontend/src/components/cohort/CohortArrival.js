import { useEffect, useRef } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useUser } from '../../contexts/UserContext';
import {
  cohortShownThisSession, markCohortShown, pendingCohortCode, shouldShowCohort,
} from '../../utils/cohort';

// Mounted with the app: an invited person (or a member) whose report step
// is not finished, who lands on Home or on the page a sign-in goes to by
// default, is sent to /cohort, once per session (this tab). From there
// they can go anywhere; nothing sends them back in the same session.
export default function CohortArrival() {
  const { user, loading } = useUser();
  const navigate = useNavigate();
  const location = useLocation();
  // The page this load arrived on, before any redirect of the app's.
  const firstPath = useRef(location.pathname);
  const done = useRef(false);
  useEffect(() => {
    if (loading || done.current) return;
    done.current = true;
    if (shouldShowCohort(user, firstPath.current, {
      shown: cohortShownThisSession(), pendingCode: pendingCohortCode(),
    })) {
      markCohortShown();
      navigate('/cohort', { replace: true });
    }
  }, [user, loading, navigate]);
  return null;
}
