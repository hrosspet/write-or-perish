// Markdown images (#441): Loore's own media loads, any other image is a
// placeholder that opens only when clicked, and links render as before.
//
// react-markdown is ESM (not transformed by CRA jest), so it is replaced by a
// minimal renderer that turns one paragraph of `![alt](src)`, `[text](href)`
// and `[![alt](src)](href)` into the `components` MarkdownBody hands it, with
// the props react-markdown passes.
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';

jest.mock('react-markdown', () => {
  const R = require('react');
  return function FakeMarkdown({ children, components }) {
    const token = /\[!\[([^\]]*)\]\(([^)]*)\)\]\(([^)]*)\)|!\[([^\]]*)\]\(([^)]*)\)|\[([^\]]*)\]\(([^)]*)\)/g;
    const text = String(children);
    const parts = [];
    let key = 0;
    const img = (alt, src) => R.createElement(components.img, { key: key++, node: { tagName: 'img' }, alt, src });
    const link = (href, kids) => R.createElement(components.a, { key: key++, node: { tagName: 'a' }, href }, kids);
    let last = 0;
    let m;
    while ((m = token.exec(text))) {
      if (m.index > last) parts.push(text.slice(last, m.index));
      if (m[1] !== undefined) parts.push(link(m[3], img(m[1], m[2])));
      else if (m[4] !== undefined) parts.push(img(m[4], m[5]));
      else parts.push(link(m[7], m[6]));
      last = token.lastIndex;
    }
    if (last < text.length) parts.push(text.slice(last));
    return R.createElement(components.p, { node: { tagName: 'p' } }, parts);
  };
});
jest.mock('remark-gfm', () => () => {});
jest.mock('react-router-dom', () => ({
  useInRouterContext: () => false,
  useNavigate: () => () => {},
}));

// eslint-disable-next-line import/first
import MarkdownBody from './MarkdownBody';

// jsdom does not navigate; keep it from reporting "not implemented" when a
// test clicks a real link.
const blockNavigation = (e) => e.preventDefault();
beforeAll(() => window.addEventListener('click', blockNavigation, true));
afterAll(() => window.removeEventListener('click', blockNavigation, true));

test("Loore's own media loads as the text renders", () => {
  const { container } = render(<MarkdownBody>{'See ![the chart](/media/user/1/chart.png) here'}</MarkdownBody>);
  const img = container.querySelector('img');
  expect(img).not.toBeNull();
  expect(img).toHaveAttribute('src', '/media/user/1/chart.png');
  expect(img).toHaveAttribute('alt', 'the chart');
  expect(screen.queryByText(/Image from/)).toBeNull();
});

test('an outside image is a placeholder with its host and alt text, not an <img>', () => {
  const onCardClick = jest.fn();
  const { container } = render(
    <div onClick={onCardClick}>
      <MarkdownBody>{'![a cat](https://evil.example/p.png?q=private%20text)'}</MarkdownBody>
    </div>,
  );
  expect(container.querySelector('img')).toBeNull();
  const placeholder = screen.getByRole('link', { name: 'Image from evil.example: a cat' });
  // Clicking opens that one image in a new tab, without a referrer.
  expect(placeholder).toHaveAttribute('href', 'https://evil.example/p.png?q=private%20text');
  expect(placeholder).toHaveAttribute('target', '_blank');
  expect(placeholder).toHaveAttribute('rel', 'noopener noreferrer');

  fireEvent.click(placeholder);
  // Nothing loads in place, and the click does not also open the card around it.
  expect(container.querySelector('img')).toBeNull();
  expect(onCardClick).not.toHaveBeenCalled();
});

test('an outside image without alt text still names its host', () => {
  render(<MarkdownBody>{'![](https://cdn.example.org/x.gif)'}</MarkdownBody>);
  expect(screen.getByRole('link', { name: 'Image from cdn.example.org' })).toBeInTheDocument();
});

test('an outside image inside a link is plain text in that link, not a second link', () => {
  const { container } = render(
    <MarkdownBody>{'[![build status](https://badges.example/b.svg)](https://github.com/o/r)'}</MarkdownBody>,
  );
  expect(container.querySelector('img')).toBeNull();
  const links = container.querySelectorAll('a');
  expect(links).toHaveLength(1);
  expect(links[0]).toHaveAttribute('href', 'https://github.com/o/r');
  expect(links[0]).toHaveTextContent('Image from badges.example: build status');
});

test('an image source that is not a web URL shows its alt text only', () => {
  const { container } = render(<MarkdownBody>{'![just words]()'}</MarkdownBody>);
  expect(container.querySelector('img')).toBeNull();
  expect(container.querySelector('a')).toBeNull();
  expect(screen.getByText('just words')).toBeInTheDocument();
});

test('links render as before: outside links open in a new tab, app paths in place', () => {
  const onInternal = jest.fn();
  render(
    <MarkdownBody onInternalLinkClick={onInternal}>
      {'Read [the page](https://example.com/page) or [your account](/account).'}
    </MarkdownBody>,
  );
  const outside = screen.getByRole('link', { name: 'the page' });
  expect(outside).toHaveAttribute('href', 'https://example.com/page');
  expect(outside).toHaveAttribute('target', '_blank');
  expect(outside).toHaveAttribute('rel', 'noopener noreferrer');

  const inside = screen.getByRole('link', { name: 'your account' });
  expect(inside).toHaveAttribute('href', '/account');
  expect(inside).not.toHaveAttribute('target');
  fireEvent.click(inside);
  expect(onInternal).toHaveBeenCalledWith('/account');
});
