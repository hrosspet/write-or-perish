import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import api from '../../api';
import MarkdownBody from '../MarkdownBody';
import MarkdownEditor from './MarkdownEditor';
import useNodeText from './useNodeText';
import { DRAFT_POLL_MS } from './CohortSteps';
import {
  Section, accentButton, plainButton, h1Style, textStyle, smallStyle,
  errorStyle, readersStyle, rowStyle, accentLinkStyle, linkStyle,
} from './cohortUi';
import {
  DRAFTING, callTimeText, errorSentence, onDayText,
} from '../../utils/cohort';
import { sharedDate } from '../../pages/SharedWithYouPage';
import { atName } from '../../utils/circles';

// The group page (/cohort for admitted members and the founder): the
// people with their introductions, the question of the week, the next
// call in the viewer's own time zone, and what members shared with the
// group, newest first, each opening the normal entry page. A member
// without an introduction is offered one drafted from their report.

export const laterReadersSentence = (name) =>
  `Everyone in ${name} can read it, including people who join the group later.`;

// ── The introduction ────────────────────────────────────────────────────

export function IntroStep({ cohortName, user, onShared }) {
  const [intro, setIntro] = useState(null);
  const [loadError, setLoadError] = useState('');
  // Writing one without a draft: the text lives here until it is shared.
  const [own, setOwn] = useState(null);
  // The private entry made for it on a first try ({ id, text }): a retry
  // after a refused share updates it instead of making another.
  const ownEntry = useRef(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const nodeText = useNodeText(intro && intro.node_id);

  const load = useCallback(() => api.get('/cohort/intro')
    .then((res) => { setIntro(res.data.intro); return res.data.intro; })
    .catch(() => setLoadError('Your introduction could not be loaded. Reload the page to try again.')),
  []);

  useEffect(() => { load(); }, [load]);

  const drafting = !!intro && DRAFTING.has(intro.task_status);
  useEffect(() => {
    if (!drafting) return undefined;
    const timer = setInterval(() => {
      api.get('/cohort/intro').then((res) => {
        if (!DRAFTING.has(res.data.intro.task_status)) setIntro(res.data.intro);
      }).catch(() => {});
    }, DRAFT_POLL_MS);
    return () => clearInterval(timer);
  }, [drafting]);

  const run = async (fn) => {
    setBusy(true);
    setError('');
    try {
      await fn();
    } catch (err) {
      setError(errorSentence(err));
    }
    setBusy(false);
  };

  const draft = () => run(async () => {
    const res = await api.post('/cohort/intro/draft');
    setIntro(res.data.intro);
  });

  const share = () => run(async () => {
    let nodeId = intro && intro.node_id;
    if (own !== null && ownEntry.current) {
      // A second try: the entry exists; it gets the text as it is now.
      const made = ownEntry.current;
      if (made.text !== own) {
        await api.put(`/nodes/${made.id}`, { content: own, base_content: made.text });
        ownEntry.current = { id: made.id, text: own };
      }
      nodeId = made.id;
    } else if (own !== null) {
      const res = await api.post('/nodes/', {
        content: own,
        privacy_level: 'private',
        ai_usage: (user && user.default_ai_usage) || 'none',
      });
      nodeId = res.data.id;
      ownEntry.current = { id: nodeId, text: own };
    } else {
      await nodeText.save();
    }
    const res = await api.post('/cohort/intro', { node_id: nodeId, share: true });
    ownEntry.current = null;
    setOwn(null);
    setIntro(res.data.intro);
    onShared();
  });

  if (loadError) return <p role="alert" style={errorStyle}>{loadError}</p>;
  if (!intro || intro.shared_with_group) return null;

  const readers = <p style={readersStyle}>{laterReadersSentence(cohortName)}</p>;
  const shareButton = (enabled) => (
    <div style={rowStyle}>
      <button type="button" style={accentButton} disabled={busy || !enabled} onClick={share}>
        Share with the group
      </button>
    </div>
  );

  let body;
  if (own !== null) {
    body = (
      <>
        <MarkdownEditor id="cohort-intro-own" label="Your introduction"
          value={own} onChange={setOwn} minHeight="200px" disabled={busy}
          placeholder="Who you are, what you hope for from the group, what you bring." />
        {readers}
        {shareButton(own.trim().length > 0)}
      </>
    );
  } else if (drafting) {
    body = (
      <p role="status" style={textStyle}>
        Loore is writing a draft of your introduction from your report. It appears here when it is ready.
      </p>
    );
  } else if (intro.node_id) {
    body = (
      <>
        <p style={textStyle}>
          Loore drafted this from your report. Change it as you like before you share it.
        </p>
        {nodeText.loadError && <p role="alert" style={errorStyle}>{nodeText.loadError}</p>}
        {nodeText.loaded && (
          <MarkdownEditor id="cohort-intro-text" label="Your introduction"
            value={nodeText.text} onChange={nodeText.setText} minHeight="200px"
            disabled={busy} />
        )}
        {readers}
        {shareButton(nodeText.loaded && nodeText.text.trim().length > 0)}
      </>
    );
  } else {
    const nothing = intro.task_status === 'nothing_to_draft';
    body = (
      <>
        <p style={textStyle}>
          {intro.task_status === 'failed'
            ? 'Loore could not write the draft.'
            : nothing
              ? 'Your report is empty, so Loore has nothing to draft from.'
              : 'Introduce yourself to the others. Loore can draft an introduction from your report, which you then change as you like.'}
        </p>
        <div style={rowStyle}>
          {!nothing && (
            <button type="button" style={accentButton} disabled={busy} onClick={draft}>
              Draft my introduction from my report
            </button>
          )}
          <button type="button" style={plainButton} disabled={busy} onClick={() => setOwn('')}>
            Write it myself
          </button>
        </div>
      </>
    );
  }

  return (
    <Section title="Your introduction" labelledBy="cohort-intro">
      {body}
      {error && <p role="alert" style={errorStyle}>{error}</p>}
    </Section>
  );
}

// ── The group page ──────────────────────────────────────────────────────

const entryMeta = (entry) => {
  const parts = [`${atName(entry.author_username)}, ${sharedDate(entry.shared_at || entry.created_at)}`];
  if (entry.last_reply_by) parts.push(`${atName(entry.last_reply_by)} replied.`);
  return parts.join('. ');
};

export default function CohortGroup({ user, slug = null }) {
  const [page, setPage] = useState(null);
  const [error, setError] = useState('');

  const load = useCallback(() => api.get('/cohort/page', { params: slug ? { cohort: slug } : {} })
    .then((res) => setPage(res.data))
    .catch((err) => setError(errorSentence(err,
      'This page could not be loaded. Reload it to try again.'))),
  [slug]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (page) document.title = `${page.cohort.name} · Loore`;
  }, [page]);

  if (error) return <p role="alert" style={errorStyle}>{error}</p>;
  if (!page) return <p style={smallStyle}>Loading…</p>;

  const { cohort, members, entries, is_founder: isFounder } = page;
  const me = members.find((m) => m.username === (user && user.username));
  const callText = callTimeText(cohort.next_call_at);

  return (
    <>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
        <h1 style={h1Style}>{cohort.name}</h1>
        {cohort.about && (
          <details>
            <summary style={{ cursor: 'pointer', fontSize: '0.94rem', color: 'var(--text-secondary)', minHeight: '32px' }}>
              What this group is
            </summary>
            <div style={{ ...textStyle, marginTop: '10px', maxWidth: 'none' }}>
              <MarkdownBody paragraphMargin="0 0 0.8em">{cohort.about}</MarkdownBody>
            </div>
          </details>
        )}
      </div>

      <Section title="This week's question" labelledBy="cohort-question">
        {cohort.question ? (
          <>
            <p style={{
              margin: 0, fontFamily: 'var(--reading-serif)', fontSize: '1.3rem',
              lineHeight: 1.5, color: 'var(--text-primary)', whiteSpace: 'pre-wrap',
            }}>
              {cohort.question}
            </p>
            {cohort.question_set_at && (
              <p style={smallStyle}>Set by {cohort.founder} {onDayText(cohort.question_set_at)}.</p>
            )}
          </>
        ) : (
          <p style={textStyle}>{cohort.founder} has not set a question yet.</p>
        )}
      </Section>

      <Section title="The next call" labelledBy="cohort-call-next">
        <p style={textStyle}>{callText || `${cohort.founder} has not set the time of the next call yet.`}</p>
        {cohort.call_url && (
          <a href={cohort.call_url} target="_blank" rel="noopener noreferrer" style={accentLinkStyle}>
            Open the call link
          </a>
        )}
        {callText && (
          <p style={smallStyle}>
            Is there something you want to bring to the call? You can write it
            in Loore and share it with the group.
          </p>
        )}
      </Section>

      {!isFounder && (
        <IntroStep cohortName={cohort.name} user={user} onShared={load} />
      )}

      <Section title="The group" labelledBy="cohort-members">
        <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
          {members.map((m, i) => (
            <li key={m.username} style={{
              display: 'flex', justifyContent: 'space-between', alignItems: 'center',
              gap: '12px', minHeight: '48px', flexWrap: 'wrap',
              borderTop: '1px solid var(--border)',
              borderBottom: i === members.length - 1 ? '1px solid var(--border)' : 'none',
            }}>
              <span style={{ fontSize: '1rem', fontWeight: 400 }}>
                {atName(m.username)}
                {m.is_founder && (
                  <span style={{ ...smallStyle, marginLeft: '10px' }}>started the group</span>
                )}
                {me && m.username === me.username && !m.is_founder && (
                  <span style={{ ...smallStyle, marginLeft: '10px' }}>you</span>
                )}
              </span>
              {m.intro_node_id ? (
                <Link to={`/node/${m.intro_node_id}`} style={linkStyle}>Introduction</Link>
              ) : (
                <span style={smallStyle}>No introduction yet</span>
              )}
            </li>
          ))}
        </ul>
      </Section>

      <Section title="Shared in the group" labelledBy="cohort-entries">
        <p style={smallStyle}>
          To share an entry with the group, choose Circles under "Who can read
          this" when you write or edit it, then {cohort.name}.{' '}
          {laterReadersSentence(cohort.name)}
        </p>
        {entries.length === 0 ? (
          <p style={textStyle}>Nothing is shared in the group yet.</p>
        ) : entries.map((entry) => (
          <Link key={entry.node_id} to={`/node/${entry.node_id}`} style={{
            textDecoration: 'none', display: 'flex', flexDirection: 'column',
            gap: '6px', minHeight: '44px', color: 'var(--text-primary)',
            paddingTop: '6px',
          }}>
            <span style={{ fontFamily: 'var(--serif)', fontWeight: 500, fontSize: '1.4rem', lineHeight: 1.25 }}>
              {entry.title || 'Untitled entry'}
            </span>
            {entry.preview && (
              <span style={{ fontSize: '0.94rem', lineHeight: 1.55, color: 'var(--text-secondary)' }}>
                {entry.preview}
              </span>
            )}
            <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
              {entryMeta(entry)}
            </span>
          </Link>
        ))}
      </Section>

      {isFounder && (
        <p style={smallStyle}>
          Invitations, the question and the call are set in <Link to="/admin" style={linkStyle}>Admin</Link>, under Cohorts.
        </p>
      )}
    </>
  );
}
