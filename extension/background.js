// Loore Clipper — service worker.
//
// One key press: read the active tab (Readability + Turndown, or the
// tweet extractor on x.com), POST it to Loore as a saved reference with
// a scoped bearer token, and close the tab so the next one is in front.
//
// Permissions are deliberately narrow: `activeTab` is granted only at the
// moment the user presses the shortcut or clicks the action, so the
// extension can never read a page on its own.

const DEFAULTS = { baseUrl: 'https://loore.org', token: '' };
const BADGE_MS = 1800;

const getSettings = () => chrome.storage.local.get(DEFAULTS);
const setLast = (last) => chrome.storage.local.set({ last });

async function badge(text, color) {
  await chrome.action.setBadgeBackgroundColor({ color });
  await chrome.action.setBadgeText({ text });
  setTimeout(() => chrome.action.setBadgeText({ text: '' }), BADGE_MS);
}

async function fail(message, extra = {}) {
  await setLast({ ok: false, message, at: Date.now(), ...extra });
  await badge('!', '#b3423f');
}

async function capture(tabId) {
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    files: ['vendor/Readability.js', 'vendor/turndown.js', 'capture.js'],
  });
  return results && results[0] ? results[0].result : null;
}

// Chrome may refuse to inject into a PDF tab at all (it has in some
// versions). The URL and the tab title are still ours under activeTab,
// and that is all the in-page capture saves for a PDF anyway.
const PDF_URL = /\.pdf(?:[?#]|$)/i;

function pdfLinkFromTab(tab) {
  let title = (tab.title || '').replace(/\.pdf$/i, '').trim();
  if (!title) {
    try {
      title = decodeURIComponent(new URL(tab.url).pathname.split('/').pop() || '')
        .replace(/\.pdf$/i, '').trim();
    } catch (e) { /* keep empty */ }
  }
  title = title || tab.url;
  return {
    url: tab.url, title, pdf: true, author: null, posted_at: null,
    content: `PDF: [${title}](${tab.url})`,
  };
}

async function clip(tab, closeAfter) {
  const { baseUrl, token } = await getSettings();
  if (!token) {
    await fail('No token yet — open the extension options and paste one.');
    chrome.runtime.openOptionsPage();
    return;
  }
  let page;
  try {
    page = await capture(tab.id);
  } catch (e) {
    if (tab.url && PDF_URL.test(tab.url)) {
      page = pdfLinkFromTab(tab);
    } else {
      await fail(`Cannot read this page (${e.message}).`, { url: tab.url });
      return;
    }
  }
  if (page && page.pdf && tab.title && !PDF_URL.test(tab.title)) {
    // Chrome titles a PDF tab from the document's own Title metadata
    // when it has one; that beats a name derived from the file name.
    page.title = tab.title.trim();
  }
  if (!page || !page.content || !page.content.trim()) {
    await fail('Nothing readable on this page.', { url: tab.url });
    return;
  }
  let res;
  try {
    res = await fetch(`${baseUrl.replace(/\/$/, '')}/api/external/clip`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(page),
    });
  } catch (e) {
    await fail(`Loore unreachable at ${baseUrl} (${e.message}).`, { url: page.url });
    return;
  }
  if (res.status === 401) {
    await fail('Loore rejected the token — create a new one on the Import page.', { url: page.url });
    chrome.runtime.openOptionsPage();
    return;
  }
  if (!res.ok) {
    let detail = '';
    try { detail = (await res.json()).error || ''; } catch (e) { /* not JSON */ }
    await fail(`Loore answered ${res.status}${detail ? `: ${detail}` : ''}.`, { url: page.url });
    return;
  }
  const data = await res.json();
  await setLast({
    ok: true, at: Date.now(), url: page.url, title: page.title,
    created: data.created, updated: !!data.updated, source: data.source,
    truncated: !!data.truncated, chars: page.content.length, pdf: !!page.pdf,
  });
  await badge(data.created || data.updated ? '✓' : '=', '#5a8f5a');
  if (closeAfter) {
    try { await chrome.tabs.remove(tab.id); } catch (e) { /* already gone */ }
  }
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tab;
}

chrome.commands.onCommand.addListener(async (command, tab) => {
  const target = tab || await activeTab();
  if (!target) return;
  await clip(target, command === 'clip-and-close');
});

// The popup's buttons route through here so the logic lives in one place.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.type !== 'clip') return false;
  activeTab()
    .then((tab) => (tab ? clip(tab, !!msg.close) : fail('No active tab.')))
    .then(() => sendResponse({ done: true }))
    .catch((e) => fail(e.message).then(() => sendResponse({ done: false })));
  return true; // async sendResponse
});
