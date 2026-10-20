// Run from the repo root (jsdom comes from the frontend's node_modules):
//   NODE_PATH=frontend/node_modules node --test extension/test/turndown-rules.test.js
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const vm = require('vm');

const { window } = new JSDOM('');
globalThis.DOMParser = window.DOMParser;
globalThis.document = window.document;
const TurndownService = vm.runInThisContext(
  fs.readFileSync(path.join(__dirname, '../vendor/turndown.js'), 'utf8') + '\n;TurndownService');
const rules = require('../turndown-rules.js');

function convert(html) {
  const td = new TurndownService({ headingStyle: 'atx', codeBlockStyle: 'fenced', bulletListMarker: '-' });
  return rules(td).turndown(html);
}

const SRC = 'https://cdn.example.com/image/fetch/w_1456/https%3A%2F%2Fmedia.example%2Fpic.jpeg';
const HREF = 'https://cdn.example.com/image/fetch/https%3A%2F%2Fmedia.example%2Fpic.jpeg';

test('a Substack-style image link is one line', () => {
  const html = `<p>Before</p><a class="image-link" href="${HREF}"><div class="image2-inset"><picture>` +
    `<source srcset="${SRC} 1456w"><img src="${SRC}" alt="A pic"></picture></div></a><p>After</p>`;
  const md = convert(html);
  assert.ok(md.includes(`[![A pic](${SRC})](${HREF})`), md);
  assert.ok(!/\[\s*\n/.test(md), md);
});

test('a caption inside the link joins on one line', () => {
  const html = `<a href="${HREF}"><div><img src="${SRC}" alt=""></div><div>Caption text</div></a>`;
  assert.strictEqual(convert(html), `[![](${SRC}) Caption text](${HREF})`);
});

test('plain links are unchanged', () => {
  assert.strictEqual(convert('<p>See <a href="https://a.example/x" title="T">this</a>.</p>'),
    'See [this](https://a.example/x "T").');
});
