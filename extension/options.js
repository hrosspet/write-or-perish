const DEFAULTS = { baseUrl: 'https://loore.org', token: '' };
const $ = (id) => document.getElementById(id);

function show(text, cls) {
  const el = $('status');
  el.textContent = text;
  el.className = cls || '';
}

function syncImportLink() {
  $('importLink').href = `${$('baseUrl').value}/import`;
}

async function load() {
  const s = await chrome.storage.local.get(DEFAULTS);
  $('baseUrl').value = s.baseUrl;
  $('token').value = s.token;
  syncImportLink();
}

async function save() {
  const baseUrl = $('baseUrl').value;
  const token = $('token').value.trim();
  await chrome.storage.local.set({ baseUrl, token });
  show('Saved.', 'ok');
}

async function test() {
  await save();
  const { baseUrl, token } = await chrome.storage.local.get(DEFAULTS);
  if (!token) { show('Paste a token first.', 'err'); return; }
  show('Checking…');
  try {
    const res = await fetch(`${baseUrl}/api/external/clip/status`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (res.status === 401) { show('Token rejected. Create a new one on the Import page.', 'err'); return; }
    if (!res.ok) { show(`Loore answered ${res.status}.`, 'err'); return; }
    const d = await res.json();
    const clips = (d.counts && d.counts.web_clip) || 0;
    show(`Connected as ${d.username} · ${clips} clipped page${clips === 1 ? '' : 's'} so far.`, 'ok');
  } catch (e) {
    show(`Cannot reach ${baseUrl} (${e.message}).`, 'err');
  }
}

$('save').addEventListener('click', save);
$('test').addEventListener('click', test);
$('baseUrl').addEventListener('change', syncImportLink);
load();
