// Deleting an artifact (#603): the ⋮ menu at the end of the meta line has
// Edit for every artifact and Delete only for custom ones; Delete asks
// with the node-deletion dialog, then the tab to the right is selected
// (the left one when it was the last) and the URL follows; a link to a
// deleted artifact says so instead of opening an empty one.
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
import api from '../api';
import ArtifactsPage from './ArtifactsPage';

jest.mock('../components/MarkdownBody', () => ({ children }) =>
  require('react').createElement('div', null, children));
jest.mock('../components/IntentionsView', () => () => null);
jest.mock('../components/VersionHistoryDrawer', () => () => null);
jest.mock('../components/ArtifactsNav', () => () => null);
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: jest.fn(), post: jest.fn(), put: jest.fn(), patch: jest.fn(), delete: jest.fn(),
  },
}));
const mockAddToast = jest.fn();
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: mockAddToast }),
}));

const row = (kind, title, extra = {}) => ({
  id: title.length, kind, title, content: `${title} body`,
  description: `About ${title}`, generated_by: 'user',
  created_at: '2026-10-28T10:00:00Z', privacy_level: 'private',
  ai_usage: 'chat', deletable: false, ...extra,
});

// The list as GET /api/artifacts/ returns it (built-ins + custom).
const LIST = [
  row('intentions', 'Intentions'),
  row('memory', 'Memory'),
  row('ai_preferences', 'AI Interaction Preferences'),
  row('landlord-case', 'Landlord Dispute', { deletable: true }),
  row('loore-map', 'Loore Development Map', { deletable: true }),
  row('external_digest', 'Saved References Digest'),
  row('winter-solstice', 'Winter Solstice Gathering', { deletable: true }),
];

let listResponse;

function Location() {
  const loc = useLocation();
  return <div data-testid="location">{loc.pathname}</div>;
}

function renderAt(path) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/artifacts/:kind" element={<><ArtifactsPage /><Location /></>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  listResponse = { artifacts: LIST, deleted_kinds: [] };
  api.get.mockImplementation((url) => {
    if (url === '/artifacts') return Promise.resolve({ data: listResponse });
    if (url.endsWith('/versions')) {
      return Promise.resolve({ data: { versions: [{ id: 1 }, { id: 2 }] } });
    }
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
  api.post.mockResolvedValue({ data: {} });
  api.delete.mockResolvedValue({ data: { deleted: 'x', versions: 2, grace_days: 30 } });
});

const openMenu = async () => {
  fireEvent.click(await screen.findByRole('button', { name: 'More actions' }));
};

test('a custom artifact has Edit and Delete in its ⋮ menu', async () => {
  renderAt('/artifacts/landlord-case');
  await openMenu();
  expect(screen.getByText('Edit')).toBeInTheDocument();
  expect(screen.getByText('Delete')).toBeInTheDocument();
});

test.each([
  ['memory'], ['intentions'], ['ai_preferences'], ['external_digest'],
])('the %s artifact has Edit only', async (kind) => {
  renderAt(`/artifacts/${kind}`);
  await openMenu();
  expect(screen.getByText('Edit')).toBeInTheDocument();
  expect(screen.queryByText('Delete')).not.toBeInTheDocument();
});

test('Edit in the menu opens the edit form', async () => {
  renderAt('/artifacts/landlord-case');
  await openMenu();
  fireEvent.click(screen.getByText('Edit'));
  expect(await screen.findByDisplayValue('Landlord Dispute body')).toBeInTheDocument();
  expect(screen.getByDisplayValue('About Landlord Dispute')).toBeInTheDocument();
});

test('Delete asks first, and Cancel deletes nothing', async () => {
  renderAt('/artifacts/landlord-case');
  await openMenu();
  fireEvent.click(screen.getByText('Delete'));
  expect(screen.getByText('Delete artifact?')).toBeInTheDocument();
  expect(screen.getByText(/and its version history will be removed\. The AI will no longer see it\./))
    .toBeInTheDocument();
  // Like node deletion, the dialog says nothing about the 30 days.
  expect(screen.queryByText(/30 days/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByText('Cancel'));
  expect(screen.queryByText('Delete artifact?')).not.toBeInTheDocument();
  expect(api.delete).not.toHaveBeenCalled();
});

test('a confirmed delete selects the tab to the right, with a toast', async () => {
  renderAt('/artifacts/landlord-case');
  await openMenu();
  fireEvent.click(screen.getByText('Delete'));
  // The dialog's Delete button (the menu is closed by now).
  fireEvent.click(screen.getByRole('button', { name: 'Delete' }));

  await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/artifacts/landlord-case'));
  await waitFor(() => expect(screen.getByTestId('location'))
    .toHaveTextContent('/artifacts/loore-map'));
  expect(mockAddToast).toHaveBeenCalledWith('Landlord Dispute deleted', 3000);
  expect(await screen.findByRole('heading', { name: 'Loore Development Map' })).toBeInTheDocument();
});

test('deleting the last tab selects the one to its left', async () => {
  renderAt('/artifacts/winter-solstice');
  await openMenu();
  fireEvent.click(screen.getByText('Delete'));
  fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
  await waitFor(() => expect(screen.getByTestId('location'))
    .toHaveTextContent('/artifacts/external_digest'));
  expect(mockAddToast).toHaveBeenCalledWith('Winter Solstice Gathering deleted', 3000);
});

test('an artifact already deleted elsewhere counts as deleted here too', async () => {
  // Deleted in another tab or on the phone since this page loaded.
  api.delete.mockRejectedValue({
    response: { status: 404, data: { error: 'This artifact was deleted.', deleted: true } },
  });
  renderAt('/artifacts/landlord-case');
  await openMenu();
  fireEvent.click(screen.getByText('Delete'));
  fireEvent.click(screen.getByRole('button', { name: 'Delete' }));

  await waitFor(() => expect(screen.getByTestId('location'))
    .toHaveTextContent('/artifacts/loore-map'));
  expect(mockAddToast).toHaveBeenCalledWith('Landlord Dispute deleted', 3000);
  expect(mockAddToast).not.toHaveBeenCalledWith('This artifact was deleted.', 4000);
  expect(await screen.findByRole('heading', { name: 'Loore Development Map' })).toBeInTheDocument();
  expect(screen.queryByText('Landlord Dispute body')).not.toBeInTheDocument();
  // The list is fetched again, as after a delete made here.
  await waitFor(() => expect(api.get.mock.calls.filter(([u]) => u === '/artifacts')).toHaveLength(2));
});

test('a failed delete stays on the artifact and says why', async () => {
  api.delete.mockRejectedValue({ response: { data: { error: 'Artifact not found' } } });
  renderAt('/artifacts/landlord-case');
  await openMenu();
  fireEvent.click(screen.getByText('Delete'));
  fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
  await waitFor(() => expect(mockAddToast).toHaveBeenCalledWith('Artifact not found', 4000));
  expect(screen.getByTestId('location')).toHaveTextContent('/artifacts/landlord-case');
});

test('a link to a deleted artifact says it was deleted', async () => {
  listResponse = { artifacts: LIST, deleted_kinds: ['old-notes'] };
  renderAt('/artifacts/old-notes');
  expect(await screen.findByText('This artifact was deleted.')).toBeInTheDocument();
  // Not an empty artifact under that name, ready to write.
  expect(screen.queryByText(/Nothing here yet/)).not.toBeInTheDocument();
  expect(screen.queryByText(/^Write /)).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'More actions' })).not.toBeInTheDocument();
  // And no view ping names it again.
  expect(api.post).not.toHaveBeenCalled();
});

test('a link to a name never used still opens an empty artifact', async () => {
  renderAt('/artifacts/brand-new');
  expect(await screen.findByText(/Nothing here yet/)).toBeInTheDocument();
  expect(screen.queryByText('This artifact was deleted.')).not.toBeInTheDocument();
});
