// /cohort (the first cohort): the page shows the person's current step,
// from GET /api/cohort/me, and only that step's action. A code kept by the
// invitation page is claimed first.
const mockGet = jest.fn();
const mockPost = jest.fn();
const mockPut = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...a) => mockGet(...a),
    post: (...a) => mockPost(...a),
    put: (...a) => mockPut(...a),
  },
}));
let mockUser;
const mockSetUser = jest.fn();
jest.mock('../components/MarkdownBody', () => ({ children }) => <div>{children}</div>);
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: mockUser, loading: false, setUser: mockSetUser }),
}));
jest.mock('./SubscribePage', () => () => <div>SUBSCRIBE PAGE</div>);
// The new-entry form itself is tested in components/cohort/CohortSharing.test.js.
jest.mock('../components/NodeFormModal', () => (props) => (
  <div role="dialog" aria-label={props.title}>
    <span data-testid="form-props">{JSON.stringify({
      privacy: props.nodeFormProps.initialPrivacyLevel,
      circles: props.nodeFormProps.initialCircleIds,
      parent: props.nodeFormProps.parentId,
    })}</span>
    <button type="button" onClick={() => props.nodeFormProps.onSuccess({ id: 99 })}>MOCK SENT</button>
  </div>
));

import React from 'react';
import { render, screen, act, fireEvent, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import CohortPage, { COHORT_TEXT, notLetInSentence } from './CohortPage';
import { INTERVIEW_TEXT, REPORT_TEXT } from '../components/cohort/CohortSteps';
import { pendingCohortCode, rememberCohortCode } from '../utils/cohort';

const CODE = 'Q2xvY2tfY29kZV9mb3JfdGVzdGluZzEy';
const COHORT = {
  name: 'First Eight', slug: 'first-eight', about: 'Eight people.',
  founder: 'Peter', founder_username: 'hrosspet', free_until: '2027-01-08',
};

function invitation(overrides = {}) {
  return {
    id: 7, state: 'claimed', cohort: COHORT, letter: 'Dear Mara, a letter.',
    booking_url: null, claimed_at: '2026-11-27T10:00:00Z', call_done_at: null,
    admitted_at: null, declined_at: null, closed_at: null,
    interview: { node_id: null, started_at: null, tip_node_id: null, tip_is_ai: null, tip_status: null, open: null },
    report: { node_id: null, task_status: null, drafted_at: null, sent_at: null, skipped_at: null, shared_with_founder: false },
    intro: { node_id: null, task_status: null, shared_with_group: false },
    ...overrides,
  };
}

let routes;
function answer(table, url, body) {
  const hit = table[url];
  if (hit === undefined) return Promise.reject(new Error(`unexpected ${url}`));
  const value = typeof hit === 'function' ? hit(body) : hit;
  return value instanceof Error || (value && value.response)
    ? Promise.reject(value) : Promise.resolve({ data: value });
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
  mockUser = { id: 5, username: 'mara', approved: true, voice_mode_enabled: true, cohort: null };
  mockSetUser.mockReset();
  routes = { get: { '/dashboard': { user: mockUser } }, post: {}, put: {} };
  mockGet.mockReset().mockImplementation((url) => answer(routes.get, url));
  mockPost.mockReset().mockImplementation((url, body) => answer(routes.post, url, body));
  mockPut.mockReset().mockImplementation((url, body) => answer(routes.put, url, body));
});

function Where() {
  const loc = useLocation();
  return <div data-testid="where">{loc.pathname}{loc.search}</div>;
}

async function renderPage(props = {}, path = '/cohort') {
  await act(async () => {
    render(
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/cohort" element={<CohortPage {...props} />} />
          <Route path="*" element={<Where />} />
        </Routes>
      </MemoryRouter>,
    );
  });
}

const setMe = (inv, ownerOf = []) => { routes.get['/cohort/me'] = { invitation: inv, owner_of: ownerOf }; };

test('claimed: the interview to start, by voice or text, with the letter folded', async () => {
  setMe(invitation());
  await renderPage();
  expect(screen.getByRole('heading', { name: 'First Eight' })).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'The interview' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Start by voice' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Start in text' })).toBeInTheDocument();
  expect(screen.getByText('Your letter from Peter')).toBeInTheDocument();
  const steps = screen.getByRole('list', { name: 'Steps' });
  expect(within(steps).getByText('The interview').closest('li')).toHaveAttribute('aria-current', 'step');
  // Only this step's action.
  expect(screen.queryByRole('button', { name: /I'm done/ })).toBeNull();
  expect(screen.queryByRole('button', { name: /Send to/ })).toBeNull();
});

test('no voice mode: text only', async () => {
  mockUser = { ...mockUser, voice_mode_enabled: false };
  setMe(invitation());
  await renderPage();
  expect(screen.queryByRole('button', { name: 'Start by voice' })).toBeNull();
  expect(screen.getByRole('button', { name: 'Start in text' })).toBeInTheDocument();
});

test('Start in text starts the interview and opens the thread page', async () => {
  setMe(invitation());
  routes.post['/cohort/interview/start'] = {
    created: true, interview_node_id: 123, mode: 'text', opening_node_id: 124,
    open: { text: '/node/124?awaitLlm=124', voice: '/voice?resume=124' },
  };
  await renderPage();
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Start in text' }));
  });
  expect(mockPost).toHaveBeenCalledWith('/cohort/interview/start', { mode: 'text' });
  expect(screen.getByTestId('where')).toHaveTextContent('/node/124?awaitLlm=124');
});

test('No AI: the interview is refused with a sentence, and the report can be written by hand or skipped', async () => {
  setMe(invitation());
  routes.post['/cohort/interview/start'] = {
    response: { status: 403, data: { error: "Your account is set to No AI, so Loore can't hold the interview.", code: 'ai_usage_none' } },
  };
  await renderPage();
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Start in text' }));
  });
  expect(screen.getByRole('alert')).toHaveTextContent('set to No AI');
  expect(screen.getByRole('button', { name: 'Write the report myself' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Send nothing to Peter' })).toBeInTheDocument();
});

test('interview begun: continue it where it is, or finish it', async () => {
  setMe(invitation({
    state: 'interview_started',
    interview: { node_id: 123, started_at: '2026-11-27T10:00:00Z', tip_node_id: 130, open: { text: '/node/130', voice: '/voice?parent=130' } },
  }));
  routes.post['/cohort/report/draft'] = { report: { node_id: null, task_status: 'pending' } };
  await renderPage();
  expect(screen.getByRole('button', { name: 'Continue by voice' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: "I'm done: write the report" })).toBeInTheDocument();
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: "I'm done: write the report" }));
  });
  expect(mockPost).toHaveBeenCalledWith('/cohort/report/draft', {});
  // The page reads the invitation again.
  expect(mockGet.mock.calls.filter(([u]) => u === '/cohort/me')).toHaveLength(2);
});

test('continue in voice opens the Voice page under the thread\'s tip', async () => {
  setMe(invitation({
    state: 'interview_started',
    interview: { node_id: 123, tip_node_id: 130, open: { text: '/node/130', voice: '/voice?parent=130' } },
  }));
  await renderPage();
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Continue by voice' }));
  });
  expect(screen.getByTestId('where')).toHaveTextContent('/voice?parent=130');
  expect(mockPost).not.toHaveBeenCalled();
});

test('report being written: says so and nothing else', async () => {
  setMe(invitation({
    state: 'interview_started', interview: { node_id: 123, open: null },
    report: { node_id: null, task_status: 'processing' },
  }));
  await renderPage();
  expect(screen.getByText(REPORT_TEXT.drafting)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /Send/ })).toBeNull();
});

test('report drafted: the editor, the one reader named, Send to Peter and Send nothing', async () => {
  setMe(invitation({
    state: 'report_drafted', interview: { node_id: 123, open: null },
    report: { node_id: 140, task_status: 'done', sent_at: null, skipped_at: null, shared_with_founder: false },
  }));
  routes.get['/nodes/140'] = { id: 140, content: '## Who I am now\n\nDraft text' };
  routes.put['/nodes/140'] = { id: 140 };
  routes.post['/cohort/report/send'] = { report: {}, booking_url: 'https://cal.example' };
  await renderPage();
  const box = screen.getByLabelText('Report');
  expect(box.value).toBe('## Who I am now\n\nDraft text');
  expect(screen.getByText('Only Peter will be able to read this. You can take it back later.'))
    .toBeInTheDocument();
  fireEvent.change(box, { target: { value: '## Who I am now\n\nMy own words' } });
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Send to Peter' }));
  });
  // The edit is saved first, on the text the editor loaded, then sent.
  expect(mockPut).toHaveBeenCalledWith('/nodes/140', {
    content: '## Who I am now\n\nMy own words',
    base_content: '## Who I am now\n\nDraft text',
  });
  expect(mockPost).toHaveBeenCalledWith('/cohort/report/send');
  expect(mockPut.mock.invocationCallOrder[0]).toBeLessThan(
    mockPost.mock.invocationCallOrder[mockPost.mock.calls.findIndex(([u]) => u === '/cohort/report/send')]);
});

test('Send nothing shares nothing', async () => {
  setMe(invitation({
    state: 'report_drafted', interview: { node_id: 123, open: null },
    report: { node_id: 140, task_status: 'done' },
  }));
  routes.get['/nodes/140'] = { id: 140, content: 'Draft' };
  routes.post['/cohort/report/skip'] = { report: {}, booking_url: null };
  await renderPage();
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Send nothing' }));
  });
  expect(mockPost).toHaveBeenCalledWith('/cohort/report/skip');
  expect(mockPost).not.toHaveBeenCalledWith('/cohort/report/send');
  expect(mockPut).not.toHaveBeenCalled();
});

test('a failed draft offers asking again, writing it oneself, or sending nothing', async () => {
  setMe(invitation({
    state: 'interview_started', interview: { node_id: 123, open: null },
    report: { node_id: null, task_status: 'failed' },
  }));
  await renderPage();
  expect(screen.getByText(REPORT_TEXT.failed)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Ask again' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Write it myself' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Send nothing' })).toBeInTheDocument();
});

test('after Send: the calendar link, and the report can be taken back', async () => {
  setMe(invitation({
    state: 'report_sent', booking_url: 'https://cal.example/peter',
    report: { node_id: 140, task_status: 'done', sent_at: '2026-11-27T11:00:00Z', shared_with_founder: true },
  }));
  routes.get['/nodes/140'] = { id: 140, content: 'Report' };
  routes.put['/nodes/140'] = { id: 140 };
  await renderPage();
  expect(screen.getByRole('heading', { name: 'A call with Peter' })).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Choose a time' })).toHaveAttribute('href', 'https://cal.example/peter');
  expect(screen.getByText('Only Peter can read your report.')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Take it back' }));
  const dialog = screen.getByRole('dialog');
  await act(async () => {
    fireEvent.click(within(dialog).getByRole('button', { name: 'Take it back' }));
  });
  expect(mockPut).toHaveBeenCalledWith('/nodes/140', {
    content: 'Report', base_content: 'Report', privacy_level: 'private',
  });
});

test('admitted: the group page with members, the question, the call and the entries', async () => {
  mockUser = { ...mockUser, cohort: { name: 'First Eight', role: 'member', report_step_done: true } };
  setMe(invitation({ state: 'admitted', admitted_at: '2026-11-29T10:00:00Z', report: { node_id: 140, sent_at: '2026-11-27T11:00:00Z' } }));
  routes.get['/cohort/page'] = {
    cohort: { ...COHORT, id: 1, question: 'What changed for you this week?', question_set_at: '2026-11-29T10:00:00Z', next_call_at: '2026-12-03T18:00:00Z', call_url: 'https://call.example/eight' },
    is_founder: false, circle_id: 33,
    members: [
      { username: 'hrosspet', intro_node_id: null, is_founder: true },
      { username: 'ben', intro_node_id: 151, is_founder: false },
      { username: 'mara', intro_node_id: null, is_founder: false },
    ],
    entries: [{ node_id: 160, author_username: 'ben', shared_at: '2026-11-30T10:00:00Z', created_at: '2026-11-30T10:00:00Z', title: 'My week', preview: 'It was long.', last_reply_by: null }],
  };
  routes.get['/cohort/intro'] = { intro: { node_id: null, task_status: null, shared_with_group: false } };
  routes.post['/cohort/intro/draft'] = { intro: { node_id: null, task_status: 'pending', shared_with_group: false } };
  await renderPage();
  expect(screen.getByRole('heading', { name: 'First Eight' })).toBeInTheDocument();
  expect(screen.getByText('What changed for you this week?')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Open the call link' })).toHaveAttribute('href', 'https://call.example/eight');
  expect(screen.getByText(/at \d\d:\d\d \(your time/)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Introduction' })).toHaveAttribute('href', '/node/151');
  expect(screen.getByRole('link', { name: /My week/ })).toHaveAttribute('href', '/node/160');
  // People who join later read what is shared with the group.
  expect(screen.getAllByText(/including people who join the group later/).length).toBeGreaterThan(0);
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Draft my introduction from my report' }));
  });
  expect(mockPost).toHaveBeenCalledWith('/cohort/intro/draft');
  expect(screen.getByText(/Loore is writing a draft of your introduction/)).toBeInTheDocument();
});

test('an introduction written by hand is saved as a private entry and shared with the group', async () => {
  mockUser = { ...mockUser, default_ai_usage: 'chat' };
  setMe(invitation({ state: 'admitted', admitted_at: '2026-11-29T10:00:00Z', report: { node_id: 140 } }));
  routes.get['/cohort/page'] = {
    cohort: { ...COHORT, id: 1, question: null, next_call_at: null, call_url: null },
    is_founder: false, circle_id: 33,
    members: [{ username: 'mara', intro_node_id: null, is_founder: false }],
    entries: [],
  };
  routes.get['/cohort/intro'] = { intro: { node_id: null, task_status: null, shared_with_group: false } };
  routes.post['/nodes/'] = { id: 170 };
  routes.post['/cohort/intro'] = { intro: { node_id: 170, task_status: null, shared_with_group: true } };
  await renderPage();
  fireEvent.click(screen.getByRole('button', { name: 'Write it myself' }));
  fireEvent.change(screen.getByRole('textbox', { name: 'Your introduction' }), { target: { value: 'I am Mara.' } });
  expect(screen.getByText('Everyone in First Eight can read it, including people who join the group later.'))
    .toBeInTheDocument();
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Share with the group' }));
  });
  expect(mockPost).toHaveBeenCalledWith('/nodes/', { content: 'I am Mara.', privacy_level: 'private', ai_usage: 'chat' });
  expect(mockPost).toHaveBeenCalledWith('/cohort/intro', { node_id: 170, share: true });
});

test('the founder gets the group page, without the introduction step', async () => {
  mockUser = { ...mockUser, username: 'hrosspet' };
  setMe(null, [{ id: 1, name: 'First Eight', slug: 'first-eight' }]);
  routes.get['/cohort/page'] = {
    cohort: { ...COHORT, id: 1, question: null, next_call_at: null, call_url: null },
    is_founder: true, circle_id: 30,
    members: [{ username: 'hrosspet', intro_node_id: null, is_founder: true }],
    entries: [],
  };
  await renderPage();
  expect(screen.getByRole('heading', { name: 'First Eight' })).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Your introduction' })).toBeNull();
  expect(mockGet).not.toHaveBeenCalledWith('/cohort/intro');
});

test('a kept code is claimed, forgotten, and the next step shows', async () => {
  rememberCohortCode(CODE);
  setMe(null);
  routes.post['/cohort/claim'] = { invitation: invitation() };
  await renderPage();
  expect(mockPost).toHaveBeenCalledWith('/cohort/claim', { code: CODE });
  expect(pendingCohortCode()).toBeNull();
  expect(screen.getByRole('button', { name: 'Start in text' })).toBeInTheDocument();
  // The user is read again: the server now names the cohort.
  expect(mockGet).toHaveBeenCalledWith('/dashboard', { params: { profile: 0 } });
  expect(mockSetUser).toHaveBeenCalled();
});

test('a claim with the wrong X account drops the code and says how to fix it', async () => {
  rememberCohortCode(CODE);
  setMe(null);
  routes.post['/cohort/claim'] = {
    response: { status: 403, data: { error: 'This invitation is for @mara. Sign in with that X account to accept it.', code: 'cohort_wrong_account' } },
  };
  await renderPage();
  expect(screen.getByRole('alert')).toHaveTextContent('This invitation is for @mara.');
  expect(screen.getByRole('link', { name: 'Sign out' })).toBeInTheDocument();
  // Another account signing in on this browser is not sent to /cohort for
  // it; opening the link again keeps it anew.
  expect(pendingCohortCode()).toBeNull();
});

test('a claim of a closed invitation drops the code', async () => {
  rememberCohortCode(CODE);
  setMe(null);
  routes.post['/cohort/claim'] = {
    response: { status: 409, data: { error: 'This invitation is no longer open.', code: 'cohort_not_open' } },
  };
  await renderPage();
  expect(screen.getByRole('alert')).toHaveTextContent('This invitation is no longer open.');
  expect(pendingCohortCode()).toBeNull();
});

test('no invitation, no code: one sentence', async () => {
  setMe(null);
  await renderPage();
  expect(screen.getByText(COHORT_TEXT.none)).toBeInTheDocument();
});

test('declined: one sentence', async () => {
  setMe(invitation({ state: 'declined', declined_at: '2026-11-27T10:00:00Z' }));
  await renderPage();
  expect(screen.getByText(COHORT_TEXT.declined)).toBeInTheDocument();
});

test('not let in yet (bare), no code: the subscribe page', async () => {
  setMe(null);
  await renderPage({ bare: true });
  expect(screen.getByText('SUBSCRIBE PAGE')).toBeInTheDocument();
  expect(mockPost).not.toHaveBeenCalled();
});

test('not let in yet (bare): a kept code is claimed, and the user read again before anything else shows', async () => {
  rememberCohortCode(CODE);
  setMe(null);
  routes.post['/cohort/claim'] = { invitation: invitation({ cohort: { ...COHORT, free_until: null } }) };
  await renderPage({ bare: true });
  expect(mockPost).toHaveBeenCalledWith('/cohort/claim', { code: CODE });
  expect(mockGet).toHaveBeenCalledWith('/dashboard', { params: { profile: 0 } });
  expect(mockSetUser).toHaveBeenCalled();
  expect(pendingCohortCode()).toBeNull();
  // Still not let in (no free period set): one sentence says why, over the
  // subscribe page.
  expect(screen.getByRole('status')).toHaveTextContent(
    "You accepted the invitation to First Eight, but its free period is not set yet");
  expect(screen.getByText('SUBSCRIBE PAGE')).toBeInTheDocument();
});

test('notLetInSentence says why an accepted invitation does not let the account in', () => {
  const today = new Date('2027-01-10T12:00:00Z');
  expect(notLetInSentence(invitation({ cohort: { ...COHORT, free_until: '2027-01-08' } }), today))
    .toBe('You accepted the invitation to First Eight. Its free period ended on 8 January 2027, so Loore now asks for a subscription.');
  expect(notLetInSentence(invitation({ state: 'closed' }), today)).toBeNull();
  expect(notLetInSentence(null, today)).toBeNull();
});

// ── From the frontend review ────────────────────────────────────────────

const drafted = (report = {}) => invitation({
  state: 'report_drafted',
  interview: { node_id: 123, open: { text: '/node/130', voice: '/voice?parent=130' } },
  report: { node_id: 140, task_status: 'done', sent_at: null, skipped_at: null, shared_with_founder: false, written_by_loore: true, ...report },
});

test('the call step never sends: "Read and send the report" opens the editor, and Send there sends', async () => {
  // After Send nothing: the report exists and Peter can't read it.
  setMe(invitation({
    state: 'report_skipped', booking_url: 'https://cal.example/peter',
    interview: { node_id: 123, open: null },
    report: { node_id: 140, task_status: 'done', skipped_at: '2026-11-27T11:00:00Z', shared_with_founder: false, written_by_loore: true },
  }));
  routes.get['/nodes/140'] = { id: 140, content: 'The draft she never read' };
  routes.post['/cohort/report/send'] = { report: {}, booking_url: null };
  await renderPage();
  expect(screen.getByText("Peter can't read your report.")).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Send to Peter' })).toBeNull();
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Read and send the report' }));
  });
  expect(mockPost).not.toHaveBeenCalled();
  // The text is on screen, with its only reader named.
  expect(screen.getByRole('textbox', { name: 'Report' }).value).toBe('The draft she never read');
  expect(screen.getByText('Only Peter will be able to read this. You can take it back later.')).toBeInTheDocument();
  // Back without sending.
  fireEvent.click(screen.getByRole('button', { name: 'Back' }));
  expect(screen.getByRole('heading', { name: 'A call with Peter' })).toBeInTheDocument();
  expect(mockPost).not.toHaveBeenCalled();
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Read and send the report' }));
  });
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Send to Peter' }));
  });
  expect(mockPost).toHaveBeenCalledWith('/cohort/report/send');
});

test('a save refused because the text changed elsewhere stops Send', async () => {
  setMe(drafted());
  routes.get['/nodes/140'] = { id: 140, content: 'Draft' };
  routes.put['/nodes/140'] = {
    response: { status: 409, data: { error: "There's a newer version of this text, so this change wasn't saved. Reload the page before continuing.", code: 'content_changed' } },
  };
  routes.post['/cohort/report/send'] = { report: {} };
  await renderPage();
  fireEvent.change(screen.getByRole('textbox', { name: 'Report' }), { target: { value: 'Draft, edited' } });
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Send to Peter' }));
  });
  expect(mockPut).toHaveBeenCalled();
  expect(mockPost).not.toHaveBeenCalledWith('/cohort/report/send');
  expect(screen.getByRole('alert')).toHaveTextContent("There's a newer version of this text");
});

test('"Loore wrote this draft" only over a draft a model wrote', async () => {
  setMe(drafted({ written_by_loore: true }));
  routes.get['/nodes/140'] = { id: 140, content: 'Draft' };
  await renderPage();
  expect(screen.getByText(REPORT_TEXT.intro)).toBeInTheDocument();
});

test('a report written by hand (or blank) does not say Loore wrote it', async () => {
  setMe(invitation({
    state: 'report_drafted', interview: { node_id: null, open: null },
    report: { node_id: 140, task_status: 'done', written_by_loore: false },
  }));
  routes.get['/nodes/140'] = { id: 140, content: '# My report for Peter' };
  await renderPage();
  expect(screen.queryByText(REPORT_TEXT.intro)).toBeNull();
  expect(screen.getByText(REPORT_TEXT.own('Peter'))).toBeInTheDocument();
  // No interview: nothing to continue.
  expect(screen.queryByRole('button', { name: /Continue the interview/ })).toBeNull();
});

test('the report step offers continuing the interview', async () => {
  setMe(drafted());
  routes.get['/nodes/140'] = { id: 140, content: 'Draft' };
  await renderPage();
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Continue the interview in text' }));
  });
  expect(screen.getByTestId('where')).toHaveTextContent('/node/130');
});

test('nothing to draft: continue the interview, ask again, write it, or send nothing', async () => {
  setMe(invitation({
    state: 'interview_started', interview: { node_id: 123, open: { text: '/node/130', voice: '/voice?parent=130' } },
    report: { node_id: null, task_status: 'nothing_to_draft', nothing_reason: 'empty' },
  }));
  await renderPage();
  // She said nothing: No AI is not mentioned.
  expect(screen.getByText(REPORT_TEXT.nothingSaid)).toBeInTheDocument();
  expect(screen.queryByText(/No AI/)).toBeNull();
  expect(screen.getByRole('button', { name: 'Continue the interview in text' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Ask again' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Write it myself' })).toBeInTheDocument();
});

test('the start step says how to come back, and opening the interview marks it for the link back', async () => {
  setMe(invitation());
  routes.post['/cohort/interview/start'] = {
    created: true, interview_node_id: 123, opening_node_id: 124,
    open: { text: '/node/124?awaitLlm=124', voice: '/voice?resume=124' },
  };
  await renderPage();
  expect(screen.getByText(INTERVIEW_TEXT.comeBack('First Eight'), { exact: false })).toBeInTheDocument();
  // One paragraph about the end, not two that begin alike.
  expect(screen.getAllByText(/When you are done/)).toHaveLength(1);
  expect(INTERVIEW_TEXT.comeBack('First Eight')).toBe(
    'When you are done, come back to this page with the "First Eight" link at the top of the page, and press "I\'m done: write the report".');
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Start in text' }));
  });
  expect(window.sessionStorage.getItem('loore:cohortInterview')).toBe('First Eight');
});

test('why the opening failed stays on screen after the step changes, with the way to try again', async () => {
  let calls = 0;
  routes.get['/cohort/me'] = () => {
    calls += 1;
    return {
      invitation: calls === 1 ? invitation() : invitation({
        state: 'interview_started', interview: { node_id: 123, open: null },
      }),
      owner_of: [],
    };
  };
  routes.post['/cohort/interview/start'] = {
    created: true, interview_node_id: 123, opening_node_id: null, open: null,
    llm_error: 'This thread is too long for a reply.',
  };
  await renderPage();
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Start in text' }));
  });
  // Now the interview step, and the reason is still there.
  expect(screen.getByRole('button', { name: 'Continue in text' })).toBeInTheDocument();
  expect(screen.getByRole('alert')).toHaveTextContent(
    'This thread is too long for a reply. You can try again with the buttons above.');
  // Continue tries again (no thread tip to open: the start route).
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Continue in text' }));
  });
  expect(mockPost.mock.calls.filter(([u]) => u === '/cohort/interview/start')).toHaveLength(2);
});

test('retrying "Write it myself" after a refused share makes no second entry', async () => {
  mockUser = { ...mockUser, default_ai_usage: 'chat' };
  setMe(invitation({ state: 'admitted', admitted_at: '2026-11-29T10:00:00Z', report: { node_id: 140 } }));
  routes.get['/cohort/page'] = {
    cohort: { ...COHORT, id: 1, question: null, next_call_at: null, call_url: null },
    is_founder: false, circle_id: 33,
    members: [{ username: 'mara', intro_node_id: null, is_founder: false }],
    entries: [],
  };
  routes.get['/cohort/intro'] = { intro: { node_id: null, task_status: null, shared_with_group: false } };
  routes.post['/nodes/'] = { id: 170 };
  routes.put['/nodes/170'] = { id: 170 };
  let tries = 0;
  routes.post['/cohort/intro'] = () => {
    tries += 1;
    return tries === 1
      ? { response: { status: 409, data: { error: 'The entry quotes something the group can\'t read.', code: 'quotes_not_shared' } } }
      : { intro: { node_id: 170, task_status: null, shared_with_group: true } };
  };
  await renderPage();
  fireEvent.click(screen.getByRole('button', { name: 'Write it myself' }));
  const box = screen.getByRole('textbox', { name: 'Your introduction' });
  fireEvent.change(box, { target: { value: 'I am Mara.' } });
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Share with the group' }));
  });
  expect(screen.getByRole('alert')).toHaveTextContent("can't read");
  fireEvent.change(screen.getByRole('textbox', { name: 'Your introduction' }), { target: { value: 'I am Mara, again.' } });
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Share with the group' }));
  });
  expect(mockPost.mock.calls.filter(([u]) => u === '/nodes/')).toHaveLength(1);
  expect(mockPut).toHaveBeenCalledWith('/nodes/170', { content: 'I am Mara, again.', base_content: 'I am Mara.' });
  expect(mockPost).toHaveBeenLastCalledWith('/cohort/intro', { node_id: 170, share: true });
});

// ── From the browser walk-through ───────────────────────────────────────

const groupPage = (extra = {}) => ({
  cohort: { ...COHORT, id: 1, question: null, next_call_at: null, call_url: null },
  is_founder: false, circle_id: 33,
  members: [
    { username: 'hrosspet', intro_node_id: null, is_founder: true },
    { username: 'mara', intro_node_id: null, is_founder: false },
  ],
  entries: [],
  ...extra,
});

test('a failed opening is shown as failed, with its reason and Try again', async () => {
  setMe(invitation({
    state: 'interview_started',
    interview: {
      node_id: 1, tip_node_id: 2, tip_is_ai: true, tip_status: 'failed', tip_is_opening: true,
      tip_error: 'Your monthly spending limit is reached.',
      open: { text: '/node/2', voice: '/voice?parent=2' },
    },
  }));
  routes.post['/cohort/interview/start'] = {
    created: false, interview_node_id: 1, opening_node_id: 3,
    open: { text: '/node/3?awaitLlm=3', voice: '/voice?resume=3' },
  };
  await renderPage();
  // One sentence, then the reason the server gives in words of its own.
  expect(screen.getByRole('alert').textContent).toBe(
    'Loore could not write its first message. Your monthly spending limit is reached.');
  expect(screen.queryByText(/Continue where you left off/)).toBeNull();
  expect(screen.queryByRole('button', { name: 'Continue in text' })).toBeNull();
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Try again in text' }));
  });
  // Asked for again through the start route, never by opening the failed node.
  expect(mockPost).toHaveBeenCalledWith('/cohort/interview/start', { mode: 'text' });
  expect(screen.getByTestId('where')).toHaveTextContent('/node/3?awaitLlm=3');
});

test('without a reason of its own, the failure is one sentence', async () => {
  setMe(invitation({
    state: 'interview_started',
    interview: { node_id: 1, tip_node_id: 2, tip_is_ai: true, tip_status: 'failed', tip_is_opening: true, tip_error: null, open: null },
  }));
  await renderPage();
  expect(screen.getByRole('alert').textContent).toBe('Loore could not write its first message.');
  expect(screen.getByRole('button', { name: 'Try again in text' })).toBeInTheDocument();
});

test('a later reply that failed says so too', async () => {
  setMe(invitation({
    state: 'interview_started',
    interview: { node_id: 1, tip_node_id: 9, tip_is_ai: true, tip_status: 'failed', tip_is_opening: false, tip_error: null, open: { text: '/node/9', voice: '/voice?parent=9' } },
  }));
  await renderPage();
  expect(screen.getByRole('alert')).toHaveTextContent(INTERVIEW_TEXT.replyFailed);
  expect(screen.getByRole('button', { name: 'Try again in text' })).toBeInTheDocument();
});

test('the introduction can be drafted again, behind a dialog, after its edits are saved', async () => {
  setMe(invitation({ state: 'admitted', admitted_at: '2026-11-29T10:00:00Z', report: { node_id: 140 } }));
  routes.get['/cohort/page'] = groupPage();
  routes.get['/cohort/intro'] = { intro: { node_id: 150, task_status: 'done', shared_with_group: false } };
  routes.get['/nodes/150'] = { id: 150, content: 'I teach drawing.' };
  routes.put['/nodes/150'] = { id: 150 };
  routes.post['/cohort/intro/draft'] = { intro: { node_id: 150, task_status: 'pending', shared_with_group: false } };
  await renderPage();
  fireEvent.change(screen.getByRole('textbox', { name: 'Your introduction' }), { target: { value: 'I teach drawing in Malmö.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Ask Loore for a new draft' }));
  const dialog = screen.getByRole('dialog');
  expect(mockPost).not.toHaveBeenCalledWith('/cohort/intro/draft');
  await act(async () => {
    fireEvent.click(within(dialog).getByRole('button', { name: 'Write a new draft' }));
  });
  expect(mockPut).toHaveBeenCalledWith('/nodes/150', { content: 'I teach drawing in Malmö.', base_content: 'I teach drawing.' });
  expect(mockPost).toHaveBeenCalledWith('/cohort/intro/draft');
  expect(screen.getByText(/Loore is writing a draft of your introduction/)).toBeInTheDocument();
});

test('Write to the group opens the new-entry form shared with the group\'s circle', async () => {
  setMe(invitation({ state: 'admitted', admitted_at: '2026-11-29T10:00:00Z', report: { node_id: 140 } }));
  routes.get['/cohort/page'] = groupPage();
  routes.get['/cohort/intro'] = { intro: { node_id: null, task_status: null, shared_with_group: true } };
  await renderPage();
  expect(screen.getByText(/To share an entry you wrote elsewhere, open it, choose Edit/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Write to the group' }));
  expect(screen.getByRole('dialog', { name: 'Write to First Eight' })).toBeInTheDocument();
  expect(JSON.parse(screen.getByTestId('form-props').textContent)).toEqual(
    { privacy: 'circles', circles: [33], parent: null });
  const before = mockGet.mock.calls.filter(([u]) => u === '/cohort/page').length;
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'MOCK SENT' }));
  });
  // Closed, and the group page read again (the new entry shows).
  expect(screen.queryByRole('dialog', { name: 'Write to First Eight' })).toBeNull();
  expect(mockGet.mock.calls.filter(([u]) => u === '/cohort/page').length).toBe(before + 1);
});

test('the founder\'s row shows his name, and no "No introduction yet"', async () => {
  setMe(invitation({ state: 'admitted', admitted_at: '2026-11-29T10:00:00Z', report: { node_id: 140 } }));
  routes.get['/cohort/page'] = groupPage();
  routes.get['/cohort/intro'] = { intro: { node_id: null, task_status: null, shared_with_group: true } };
  await renderPage();
  expect(screen.getByText('Peter (@hrosspet)')).toBeInTheDocument();
  expect(screen.getAllByText('No introduction yet')).toHaveLength(1);   // Mara's only
});

test('with the experiment switched off, /cohort says the page is not available', async () => {
  routes.get['/cohort/me'] = { response: { status: 404, data: { error: 'Not found' } } };
  await renderPage();
  expect(screen.getByRole('alert')).toHaveTextContent(COHORT_TEXT.unavailable);
});

test('nothing to draft because her answers are set to No AI: that is said', async () => {
  setMe(invitation({
    state: 'interview_started', interview: { node_id: 123, open: { text: '/node/130', voice: '/voice?parent=130' } },
    report: { node_id: null, task_status: 'nothing_to_draft', nothing_reason: 'no_ai' },
  }));
  await renderPage();
  expect(screen.getByText(REPORT_TEXT.nothing)).toBeInTheDocument();
});
