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
    // Kept under the fixed NavBar when the page scrolls, and aligned with
    // the thread page's left edge.
    <div style={{
      position: 'sticky', top: '56px', zIndex: 5,
      background: 'var(--bg-deep)', boxSizing: 'border-box',
      padding: '4px 12px', borderBottom: '1px solid var(--border)',
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
