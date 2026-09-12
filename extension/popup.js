const $ = (id) => document.getElementById(id);

function ago(ts) {
  const s = Math.round((Date.now() - ts) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return new Date(ts).toLocaleString();
}

async function renderLast() {
  const { last, baseUrl } = await chrome.storage.local.get({ last: null, baseUrl: 'https://loore.org' });
  $('import').href = `${baseUrl}/import`;
  const el = $('last');
  if (!last) { el.textContent = 'Nothing clipped yet.'; el.className = ''; return; }
  if (last.ok) {
    const what = last.source === 'twitter_bookmark' ? 'tweet' : (last.pdf ? 'PDF link' : 'page');
    const verb = last.created ? 'Saved' : (last.updated ? 'Updated' : 'Already saved');
    el.textContent = `${verb} ${what} “${last.title || last.url}” ${ago(last.at)}`
      + (last.truncated ? ' (truncated to 100k characters)' : '') + '.';
    el.className = 'ok';
  } else {
    el.textContent = `${last.message} (${ago(last.at)})`;
    el.className = 'err';
  }
}

function send(close) {
  $('last').textContent = 'Clipping…';
  $('last').className = '';
  chrome.runtime.sendMessage({ type: 'clip', close }, () => {
    if (close) { window.close(); return; }
    renderLast();
  });
}

$('close').addEventListener('click', () => send(true));
$('keep').addEventListener('click', () => send(false));
$('options').addEventListener('click', (e) => { e.preventDefault(); chrome.runtime.openOptionsPage(); });
chrome.storage.onChanged.addListener(renderLast);
renderLast();
