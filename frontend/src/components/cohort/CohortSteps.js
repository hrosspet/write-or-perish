import React, { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import api from '../../api';
import { CircleConfirmDialog } from '../CircleDialogs';
import MarkdownEditor from './MarkdownEditor';
import useNodeText from './useNodeText';
import {
  Section, FoldedLetter, accentButton, plainButton, quietButton,
  textStyle, smallStyle, errorStyle, readersStyle, rowStyle, accentLinkStyle,
  linkStyle,
} from './cohortUi';
import { DRAFTING, errorSentence, onDayText } from '../../utils/cohort';

// The invited person's steps before the group (docs/experiments/
// FIRST-COHORT.md, "Mara's side"): start the interview, continue it or
// finish it, the report, and the call with the founder. Each shows only
// its own action. `inv` is the invitation from GET /api/cohort/me;
// `onChanged` reads it again after anything the server records.

// How often a draft being written is asked about (heuristic).
export const DRAFT_POLL_MS = 3000;

function useDraftPoll(active, path, pick, onDone) {
  useEffect(() => {
    if (!active) return undefined;
    let alive = true;
    const timer = setInterval(() => {
      api.get(path).then((res) => {
        const status = pick(res.data);
        if (alive && !DRAFTING.has(status)) onDone();
      }).catch(() => {});
    }, DRAFT_POLL_MS);
    return () => { alive = false; clearInterval(timer); };
  }, [active, path, pick, onDone]);
}

const pickReportStatus = (data) => data && data.report && data.report.task_status;

// ── Start, continue ─────────────────────────────────────────────────────

function NoAiChoices({ founder, onChanged }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const act = async (path, body) => {
    setBusy(true);
    setError('');
    try {
      await api.post(path, body);
      await onChanged();
    } catch (err) {
      setError(errorSentence(err));
      setBusy(false);
    }
  };
  return (
    <>
      <div style={rowStyle}>
        <button type="button" style={plainButton} disabled={busy}
          onClick={() => act('/cohort/report/draft', { blank: true })}>
          Write the report myself
        </button>
        <button type="button" style={quietButton} disabled={busy}
          onClick={() => act('/cohort/report/skip')}>
          Send nothing to {founder}
        </button>
      </div>
      {error && <p role="alert" style={errorStyle}>{error}</p>}
    </>
  );
}

// Start (or continue) the interview in the existing Voice or Text page.
function useOpenInterview(onChanged) {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState('');
  const [noAi, setNoAi] = useState(false);
  const open = async (mode, knownPath) => {
    setError('');
    if (knownPath) {
      navigate(knownPath);
      return;
    }
    setBusy(mode);
    try {
      const res = await api.post('/cohort/interview/start', { mode });
      const paths = res.data && res.data.open;
      if (paths && paths[mode]) {
        navigate(paths[mode]);
        return;
      }
      setError((res.data && res.data.llm_error)
        || 'Loore could not start the interview. Try again in a moment.');
      await onChanged();
    } catch (err) {
      const data = err.response && err.response.data;
      if (data && data.code === 'ai_usage_none') setNoAi(true);
      setError(errorSentence(err));
    }
    setBusy(null);
  };
  return { open, busy, error, noAi };
}

function ModeButtons({ verb, voice, busy, onOpen, primary }) {
  return (
    <div style={rowStyle}>
      {voice && (
        <button type="button" style={primary ? accentButton : plainButton}
          disabled={!!busy} onClick={() => onOpen('voice')}>
          {busy === 'voice' ? 'Opening…' : `${verb} by voice`}
        </button>
      )}
      <button type="button" style={primary && !voice ? accentButton : plainButton}
        disabled={!!busy} onClick={() => onOpen('text')}>
        {busy === 'text' ? 'Opening…' : `${verb} in text`}
      </button>
    </div>
  );
}

export function StartStep({ inv, user, onChanged }) {
  const founder = inv.cohort.founder;
  const { open, busy, error, noAi } = useOpenInterview(onChanged);
  return (
    <>
      <Section title="The interview" labelledBy="cohort-start">
        <p style={textStyle}>
          Loore asks you questions, one at a time, and follows what you say.
          It takes about twenty minutes, and you can stop and come back later.
          The conversation stays yours: nobody else reads it, {founder} included.
        </p>
        <p style={textStyle}>
          When you are done, Loore drafts a report for {founder}. You can change
          it or empty it, and nothing is sent until you choose to send it.
        </p>
        <ModeButtons verb="Start" voice={!!user?.voice_mode_enabled} busy={busy}
          primary onOpen={(mode) => open(mode)} />
        {error && <p role="alert" style={errorStyle}>{error}</p>}
        {noAi && <NoAiChoices founder={founder} onChanged={onChanged} />}
      </Section>
      <FoldedLetter founder={founder} letter={inv.letter} />
    </>
  );
}

export function InterviewStep({ inv, user, onChanged }) {
  const founder = inv.cohort.founder;
  const interview = inv.interview || {};
  const { open, busy, error, noAi } = useOpenInterview(onChanged);
  const [drafting, setDrafting] = useState(false);
  const [draftError, setDraftError] = useState('');
  const paths = interview.open;

  const finish = async () => {
    setDrafting(true);
    setDraftError('');
    try {
      await api.post('/cohort/report/draft', {});
      await onChanged();
    } catch (err) {
      setDraftError(errorSentence(err));
      setDrafting(false);
    }
  };

  const started = onDayText(interview.started_at);
  return (
    <>
      <Section title="The interview" labelledBy="cohort-interview">
        <p style={textStyle}>
          {started ? `You started the interview ${started}. ` : ''}
          Continue where you left off. When you have said what you want to say,
          Loore writes the report from it.
        </p>
        <ModeButtons verb="Continue" voice={!!user?.voice_mode_enabled} busy={busy}
          onOpen={(mode) => open(mode, paths && paths[mode])} />
        {error && <p role="alert" style={errorStyle}>{error}</p>}
        {noAi && <NoAiChoices founder={founder} onChanged={onChanged} />}
      </Section>
      <Section labelledBy="cohort-done">
        <div style={rowStyle}>
          <button type="button" style={accentButton} disabled={drafting} onClick={finish}>
            {drafting ? 'Starting the report…' : "I'm done: write the report"}
          </button>
        </div>
        <p style={smallStyle}>
          The report is a draft for you to read and change. Nothing is sent to {founder} until you choose to.
        </p>
        {draftError && <p role="alert" style={errorStyle}>{draftError}</p>}
      </Section>
      <FoldedLetter founder={founder} letter={inv.letter} />
    </>
  );
}

// ── The report ──────────────────────────────────────────────────────────

export const REPORT_TEXT = {
  drafting: 'Loore is writing your report from the interview. It appears here when it is ready, usually within a minute or two.',
  failed: 'Loore could not write the report.',
  failedAgain: 'Loore could not write a new draft. This is your earlier one.',
  nothing: 'The interview holds nothing Loore may read for a report.',
  intro: 'Loore wrote this draft from your interview. It is your document: change anything, empty a section, or empty all of it.',
  readers: (founder) => `Only ${founder} will be able to read this. You can take it back later.`,
};

export function ReportStep({ inv, onChanged }) {
  const founder = inv.cohort.founder;
  const report = inv.report || {};
  const status = report.task_status;
  const nodeText = useNodeText(report.node_id);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [askNew, setAskNew] = useState(false);

  useDraftPoll(DRAFTING.has(status), '/cohort/report', pickReportStatus, onChanged);

  const run = async (fn) => {
    setBusy(true);
    setError('');
    try {
      await fn();
      await onChanged();
    } catch (err) {
      setError(errorSentence(err));
    }
    setBusy(false);
  };

  const draft = (body) => run(() => api.post('/cohort/report/draft', body));
  // Changes made in the editor stay the person's own, also when they send
  // nothing.
  const skip = () => run(async () => {
    if (nodeText.dirty) await nodeText.save();
    await api.post('/cohort/report/skip');
  });
  const send = () => run(async () => {
    await nodeText.save();
    await api.post('/cohort/report/send');
  });
  const save = () => run(() => nodeText.save());
  const regenerate = () => {
    setAskNew(false);
    run(async () => {
      await nodeText.save();
      await api.post('/cohort/report/draft', { regenerate: true });
    });
  };

  if (DRAFTING.has(status)) {
    return (
      <Section title="Your report" labelledBy="cohort-report">
        <p role="status" style={textStyle}>{REPORT_TEXT.drafting}</p>
      </Section>
    );
  }

  if (!report.node_id) {
    const nothing = status === 'nothing_to_draft';
    return (
      <Section title="Your report" labelledBy="cohort-report">
        <p style={textStyle}>{nothing ? REPORT_TEXT.nothing : REPORT_TEXT.failed}</p>
        <div style={rowStyle}>
          {!nothing && (
            <button type="button" style={accentButton} disabled={busy} onClick={() => draft({})}>
              Ask again
            </button>
          )}
          <button type="button" style={plainButton} disabled={busy}
            onClick={() => draft({ blank: true })}>
            Write it myself
          </button>
          <button type="button" style={quietButton} disabled={busy} onClick={skip}>
            Send nothing
          </button>
        </div>
        {error && <p role="alert" style={errorStyle}>{error}</p>}
      </Section>
    );
  }

  return (
    <Section title="Your report" labelledBy="cohort-report">
      <p style={textStyle}>{REPORT_TEXT.intro}</p>
      {status === 'failed' && <p style={smallStyle}>{REPORT_TEXT.failedAgain}</p>}
      {nodeText.loadError && <p role="alert" style={errorStyle}>{nodeText.loadError}</p>}
      {nodeText.loaded && (
        <MarkdownEditor id="cohort-report-text" label="Report"
          value={nodeText.text} onChange={nodeText.setText}
          onSubmit={save} submitEnabled={nodeText.dirty && !busy}
          shortcutHint="saves" disabled={busy} />
      )}
      {nodeText.loaded && (
        <div style={{ ...rowStyle, justifyContent: 'space-between' }}>
          <button type="button" style={plainButton}
            disabled={busy || !nodeText.dirty} onClick={save}>
            {nodeText.saving ? 'Saving…' : 'Save'}
          </button>
          <span aria-live="polite" style={smallStyle}>
            {nodeText.saved ? 'Saved.' : nodeText.dirty ? 'Not saved yet.' : ''}
          </span>
        </div>
      )}
      <p style={readersStyle}>{REPORT_TEXT.readers(founder)}</p>
      <div style={rowStyle}>
        <button type="button" style={accentButton} disabled={busy || !nodeText.loaded} onClick={send}>
          Send to {founder}
        </button>
        <button type="button" style={quietButton} disabled={busy} onClick={skip}>
          Send nothing
        </button>
      </div>
      {error && <p role="alert" style={errorStyle}>{error}</p>}
      {inv.interview && inv.interview.node_id && (
        <button type="button" disabled={busy}
          style={{ ...quietButton, alignSelf: 'flex-start', border: 0, padding: 0 }}
          onClick={() => setAskNew(true)}>
          Ask Loore for a new draft
        </button>
      )}
      <CircleConfirmDialog
        open={askNew}
        title="Ask Loore for a new draft?"
        body="Loore writes a new draft from the interview as it is now. This draft stays among your entries in the Log, with your changes."
        confirmLabel="Write a new draft"
        busy={busy}
        onClose={() => setAskNew(false)}
        onConfirm={regenerate}
      />
    </Section>
  );
}

// ── The call ────────────────────────────────────────────────────────────

export function CallStep({ inv, onChanged }) {
  const founder = inv.cohort.founder;
  const report = inv.report || {};
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [askTakeBack, setAskTakeBack] = useState(false);

  const run = async (fn) => {
    setBusy(true);
    setError('');
    try {
      await fn();
      await onChanged();
    } catch (err) {
      setError(errorSentence(err));
    }
    setBusy(false);
  };

  // Taking the report back is the normal edit to Only me.
  const takeBack = () => {
    setAskTakeBack(false);
    run(async () => {
      const res = await api.get(`/nodes/${report.node_id}`);
      const content = (res.data && res.data.content) || '';
      await api.put(`/nodes/${report.node_id}`, {
        content, base_content: content, privacy_level: 'private',
      });
    });
  };
  const sendNow = () => run(() => api.post('/cohort/report/send'));

  let reportLine;
  if (report.shared_with_founder) {
    reportLine = `Only ${founder} can read your report.`;
  } else if (report.node_id) {
    reportLine = `${founder} can't read your report. If you want, you can still send it: only ${founder} will be able to read it.`;
  } else {
    reportLine = `You sent no report.`;
  }

  return (
    <>
      <Section title={`A call with ${founder}`} labelledBy="cohort-call">
        {inv.call_done_at ? (
          <p style={textStyle}>
            Your call with {founder} is done. When {founder} adds you to the
            group, this page shows it.
          </p>
        ) : (
          <>
            {inv.booking_url ? (
              <>
                <p style={textStyle}>Choose a time for a call with {founder}.</p>
                <a href={inv.booking_url} target="_blank" rel="noopener noreferrer"
                  style={accentLinkStyle}>
                  Choose a time
                </a>
              </>
            ) : (
              <p style={textStyle}>
                {founder} has not added a calendar yet. Ask {founder} for a time.
              </p>
            )}
            <p style={textStyle}>
              The call happens outside Loore. After it, you and {founder} decide
              whether you join the group. If you do, this page shows the group:
              the other members, the question of the week and the time of the
              next call.
            </p>
          </>
        )}
      </Section>
      <Section labelledBy="cohort-report-line">
        <p style={smallStyle}>{reportLine}</p>
        {report.node_id && (
          <div style={rowStyle}>
            <Link to={`/node/${report.node_id}`} style={linkStyle}>Open your report</Link>
            {report.shared_with_founder ? (
              <button type="button" style={quietButton} disabled={busy}
                onClick={() => setAskTakeBack(true)}>
                Take it back
              </button>
            ) : (
              <button type="button" style={quietButton} disabled={busy} onClick={sendNow}>
                Send to {founder}
              </button>
            )}
          </div>
        )}
        {error && <p role="alert" style={errorStyle}>{error}</p>}
      </Section>
      <CircleConfirmDialog
        open={askTakeBack}
        title="Take your report back?"
        body={`${founder} can no longer open it, and it leaves ${founder}'s Shared with you. It stays among your entries, readable only by you.`}
        confirmLabel="Take it back"
        busy={busy}
        onClose={() => setAskTakeBack(false)}
        onConfirm={takeBack}
      />
    </>
  );
}
