// A reply form starts at the level the page holds for the parent
// (parentPrivacyLevel), whatever the account's Default privacy is, also
// when the form's own request for the parent has failed or has not
// answered yet. Outside Craft mode the selector is not drawn, so this
// starting level is the one the reply is sent with.
let mockDefaultPrivacy = 'public';
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({
    user: {
      id: 1, username: 'u', default_ai_usage: 'chat',
      default_privacy_level: mockDefaultPrivacy,
    },
  }),
}));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: jest.fn() }),
}));
jest.mock('./StreamingMicButton', () => ({ __esModule: true, default: () => null }));
const mockGet = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...a) => mockGet(...a),
    post: jest.fn(() => Promise.resolve({ data: {} })),
    put: jest.fn(() => Promise.resolve({ data: {} })),
    delete: jest.fn(() => Promise.resolve({ data: {} })),
  },
}));

import React, { createRef } from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import NodeForm from './NodeForm';

const parentFails = () => mockGet.mockRejectedValue({ response: { status: 500 } });
const parentPending = () => mockGet.mockImplementation((url) => (
  String(url).startsWith('/nodes/10')
    ? new Promise(() => {}) : Promise.reject({ response: { status: 404 } })));

const send = async (parentLevel) => {
  const ref = createRef();
  const override = jest.fn(() => Promise.resolve({ user_node_id: 5 }));
  render(
    <MemoryRouter>
      <NodeForm ref={ref} parentId={10} parentPrivacyLevel={parentLevel}
        hidePowerFeatures hideAudioUpload compact
        onSubmitOverride={override} onSuccess={jest.fn()} />
    </MemoryRouter>,
  );
  await act(async () => { await Promise.resolve(); });
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'hello' } });
  await act(async () => { await ref.current.submit(); });
  return override;
};

beforeEach(() => {
  localStorage.clear();
  mockGet.mockReset();
  mockDefaultPrivacy = 'public';
});

describe.each([
  ['failed', parentFails],
  ['has not answered', parentPending],
])('the request for the parent %s', (_name, arrange) => {
  test('under a private node the reply is sent private (Default privacy: Public)', async () => {
    arrange();
    const override = await send('private');
    expect(override).toHaveBeenCalledTimes(1);
    expect(override.mock.calls[0][0].privacy_level).toBe('private');
  });

  test('under a public node the public-reply question opens and nothing is sent (Default privacy: Private)', async () => {
    mockDefaultPrivacy = 'private';
    arrange();
    const override = await send('public');
    expect(override).not.toHaveBeenCalled();
    expect(screen.getByText('This reply will be public')).toBeTruthy();
  });
});
