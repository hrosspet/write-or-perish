import React, { useEffect } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { useUser } from '../../contexts/UserContext';
import {
  forgetInterviewOpen, interviewBackLinkName, isInterviewPage,
} from '../../utils/cohort';

// Over the Text and Voice pages while they hold the cohort interview that
// /cohort opened: a plain link back to /cohort, where "I'm done: write the
// report" is. Going to any other page drops it.
export default function InterviewBackLink() {
  const { user } = useUser();
  const { pathname } = useLocation();
  useEffect(() => {
    if (!isInterviewPage(pathname)) forgetInterviewOpen();
  }, [pathname]);
  const name = interviewBackLinkName(pathname, user);
  if (!name) return null;
  return (
    <div style={{
      maxWidth: '760px', margin: '0 auto', boxSizing: 'border-box',
      padding: '10px clamp(14px, 4vw, 24px) 0',
      fontFamily: 'var(--sans)', fontWeight: 300, fontSize: '0.9rem',
    }}>
      <Link to="/cohort" style={{
        color: 'var(--accent)', textDecoration: 'none',
        display: 'inline-flex', alignItems: 'center', minHeight: '36px',
      }}>
        Back to {name}
      </Link>
    </div>
  );
}
