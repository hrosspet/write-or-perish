// /terms and /privacy (#440): public pages, readable without signing in,
// rendered from the one source of each text.
import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import LegalPage from './LegalPage';
import LegalDocument, { renderInline, internalPath, findPlaceholders } from '../legal/LegalDocument';
import LegalLinks from '../legal/LegalLinks';
import terms from '../legal/terms.json';
import privacy from '../legal/privacy.json';

beforeAll(() => { window.scrollTo = () => {}; });

const renderAt = (path) => render(
  <MemoryRouter initialEntries={[path]}>
    <Routes>
      <Route path="/terms" element={<LegalPage kind="terms" />} />
      <Route path="/privacy" element={<LegalPage kind="privacy" />} />
    </Routes>
  </MemoryRouter>,
);

test('/terms shows the Terms and links to the Privacy Policy', () => {
  renderAt('/terms');
  expect(screen.getByRole('heading', { level: 1, name: 'Terms of Service' })).toBeTruthy();
  expect(screen.getByText(/Terms version 3\.0/)).toBeTruthy();
  // The acceptance sentence belongs to the dialog, not the public page.
  expect(screen.queryByText(/I Agree/)).toBeNull();
  const links = screen.getAllByRole('link', { name: 'Privacy Policy' });
  expect(links.every((a) => /^\/privacy(#\w+)?$/.test(a.getAttribute('href')))).toBe(true);
  expect(links.every((a) => a.getAttribute('target') === null)).toBe(true);
});

test('/privacy shows the Privacy Policy, every section with an anchor', () => {
  renderAt('/privacy');
  expect(screen.getByRole('heading', { level: 1, name: 'Privacy Policy' })).toBeTruthy();
  for (const block of privacy.blocks.filter((b) => b.type === 'sectionTitle')) {
    const heading = screen.getByRole('heading', { level: 2, name: block.text });
    expect(heading.id).toBe(block.id);
  }
  expect(screen.getByRole('link', { name: 'Terms of Service' }).getAttribute('href')).toBe('/terms');
});

test('both documents use only the block types the renderer and the iPhone app know', () => {
  const known = ['title', 'summaryBox', 'quoteBox', 'sectionTitle', 'subheading',
    'paragraph', 'bullets', 'plainList', 'table', 'rule', 'footnote'];
  for (const doc of [terms, privacy]) {
    for (const block of doc.blocks) expect(known).toContain(block.type);
  }
  expect(typeof terms.acceptNote).toBe('string');
});

test('inline text: bold, italic and links; nothing else is interpreted', () => {
  render(
    <MemoryRouter>
      <p data-testid="p">
        {renderInline('A **bold** and *quiet* word, a [page](https://loore.org/privacy#prefill), '
          + '[mail](mailto:info@loore.org), [outside](https://openai.com) and <b>no html</b> or 2 * 3.')}
      </p>
    </MemoryRouter>,
  );
  const p = screen.getByTestId('p');
  expect(p.querySelector('strong').textContent).toBe('bold');
  expect(p.querySelector('em').textContent).toBe('quiet');
  expect(screen.getByRole('link', { name: 'page' }).getAttribute('href')).toBe('/privacy#prefill');
  expect(screen.getByRole('link', { name: 'mail' }).getAttribute('href')).toBe('mailto:info@loore.org');
  const outside = screen.getByRole('link', { name: 'outside' });
  expect(outside.getAttribute('target')).toBe('_blank');
  expect(outside.getAttribute('rel')).toContain('noopener');
  expect(p.querySelector('b')).toBeNull();
  expect(p.textContent).toContain('<b>no html</b> or 2 * 3.');
});

test('links to loore.org stay on the site they are read on', () => {
  expect(internalPath('https://loore.org/terms')).toBe('/terms');
  expect(internalPath('https://www.loore.org/privacy#rights')).toBe('/privacy#rights');
  expect(internalPath('https://openai.com/policies')).toBeNull();
  expect(internalPath('mailto:info@loore.org')).toBeNull();
});

test('placeholders are found, and links are not mistaken for them', () => {
  const doc = { blocks: [
    { type: 'paragraph', text: 'Run by [OPERATOR NAME], ID [COMPANY ID]. See [the page](https://loore.org/terms).' },
    { type: 'bullets', items: ['At [ADDRESS].', 'Again [OPERATOR NAME].', 'Kept [TODO: check this] and [Address].'] },
  ] };
  expect(findPlaceholders(doc)).toEqual(
    ['[OPERATOR NAME]', '[COMPANY ID]', '[ADDRESS]', '[TODO: check this]', '[Address]']);
});

test('the renderer shows a table, a box and a plain list', () => {
  const doc = { blocks: [
    { type: 'table', header: ['Service', 'What for'], rows: [['**OpenAI**', 'AI']] },
    { type: 'summaryBox', title: 'Short', items: ['one', 'two'] },
    { type: 'plainList', items: ['Private (default)'] },
  ] };
  render(<MemoryRouter><LegalDocument doc={doc} /></MemoryRouter>);
  expect(screen.getByRole('columnheader', { name: 'Service' })).toBeTruthy();
  expect(screen.getByRole('cell', { name: 'OpenAI' })).toBeTruthy();
  expect(screen.getAllByRole('listitem')).toHaveLength(3);
});

test('the footer links go to both pages', () => {
  render(<MemoryRouter><LegalLinks /></MemoryRouter>);
  expect(screen.getByRole('link', { name: 'Terms' }).getAttribute('href')).toBe('/terms');
  expect(screen.getByRole('link', { name: 'Privacy' }).getAttribute('href')).toBe('/privacy');
});
