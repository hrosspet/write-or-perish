// Loore Clipper — runs inside the page after vendor/Readability.js and
// vendor/turndown.js. Its completion value (the IIFE's return) travels
// back to the service worker as the injection result.
//
// No top-level declarations: the shortcut can be pressed twice on the
// same tab, and a second injection must not trip over the first.
(function () {
  // Same bound as an authored Loore entry; the server truncates again.
  var MAX_CHARS = 100000;

  function handleFrom(el) {
    if (!el) return null;
    var m = el.innerText.match(/@(\w{1,15})/);
    return m ? m[1] : null;
  }

  function tweetCapture() {
    var m = location.pathname.match(/^\/(?:i\/web\/|i\/|[^/]+\/)status(?:es)?\/(\d+)/);
    if (!m) return null;
    var id = m[1];
    var articles = document.querySelectorAll('article[data-testid="tweet"]');
    var article = null;
    for (var i = 0; i < articles.length; i++) {
      // The focal tweet is the one whose timestamp links to this status.
      if (articles[i].querySelector('a[href*="/status/' + id + '"] time')) {
        article = articles[i];
        break;
      }
    }
    if (!article) article = articles[0] || null;
    if (!article) return null;

    var texts = article.querySelectorAll('[data-testid="tweetText"]');
    var textEl = texts[0] || null;
    var text = textEl ? textEl.innerText.trim() : '';
    var handle = handleFrom(article.querySelector('[data-testid="User-Name"]'));
    if (!handle) {
      var pm = location.pathname.match(/^\/([^/]+)\/status/);
      if (pm && pm[1] !== 'i') handle = pm[1];
    }
    var timeEl = article.querySelector('a[href*="/status/' + id + '"] time') ||
      article.querySelector('time[datetime]');
    var posted = timeEl ? timeEl.getAttribute('datetime') : null;

    var parts = [];
    if (text) parts.push(text);
    if (texts.length > 1) {
      var qEl = texts[1];
      var qBox = qEl.closest('[role="link"]');
      var qHandle = handleFrom(qBox && qBox.querySelector('[data-testid="User-Name"]'));
      parts.push('[Quoting @' + (qHandle || 'unknown') + ': ' + qEl.innerText.trim() + ']');
    }
    var card = article.querySelector('[data-testid="card.wrapper"]');
    if (card) {
      var lines = card.innerText.split('\n').map(function (s) { return s.trim(); }).filter(Boolean);
      var cardLink = card.querySelector('a[href]');
      var cardHref = cardLink ? cardLink.getAttribute('href') : '';
      if (lines.length) parts.push('[Link: ' + lines.join(' — ') + (cardHref && !/t\.co\//.test(cardHref) ? ' (' + cardHref + ')' : '') + ']');
    }
    var photos = article.querySelectorAll('[data-testid="tweetPhoto"] img');
    for (var p = 0; p < photos.length; p++) {
      var alt = photos[p].getAttribute('alt');
      parts.push('[photo' + (alt && alt !== 'Image' ? ': ' + alt : '') + ']');
    }
    if (article.querySelector('[data-testid="videoPlayer"]')) parts.push('[video]');
    var links = [];
    if (textEl) {
      var anchors = textEl.querySelectorAll('a[href]');
      for (var a = 0; a < anchors.length; a++) {
        var href = anchors[a].getAttribute('href') || '';
        var shown = anchors[a].innerText.trim();
        if (/^https?:\/\/t\.co\//.test(href)) {
          // X keeps the t.co redirect in href and shows the expanded
          // (possibly elided) URL as text; the text is the useful part,
          // and it is already in the tweet text above.
          continue;
        }
        if (/^https?:/.test(href) && !/(^|\.)(x|twitter)\.com\//.test(href)
            && text.indexOf(href) === -1) {
          links.push(href);
        }
      }
    }
    if (links.length) parts.push('[Links: ' + links.join(' ') + ']');

    return {
      url: 'https://x.com/i/status/' + id,
      title: null,
      content: parts.join('\n'),
      author: handle,
      posted_at: posted,
    };
  }

  // Chrome shows a PDF in its own viewer, a frame this script cannot
  // enter: the document we run in holds one <embed> and nothing else.
  // Save the link itself, titled from the file name, so the reference
  // exists and the tab can go. Re-clipping later with a fuller capture
  // upgrades the stored text (the server keeps the longer copy).
  function pdfTitleFromUrl(href) {
    var name = '';
    try {
      var path = new URL(href).pathname;
      name = decodeURIComponent(path.split('/').filter(Boolean).pop() || '');
    } catch (e) { /* keep the empty name */ }
    name = name.replace(/\.pdf$/i, '').replace(/[_+]/g, ' ').trim();
    return name;
  }

  function pdfCapture() {
    if (document.contentType !== 'application/pdf') return null;
    var title = pdfTitleFromUrl(location.href) || location.href;
    return {
      url: location.href,
      title: title,
      content: 'PDF: [' + title + '](' + location.href + ')\n\n' +
        '_Only the link was saved; the PDF\'s text was not extracted._',
      author: location.hostname.replace(/^www\./, ''),
      posted_at: null,
      pdf: true,
    };
  }

  function pageCapture() {
    var article = null;
    try {
      article = new Readability(document.cloneNode(true)).parse();
    } catch (e) { /* fall through to the plain-text path */ }
    var title = document.title || location.href;
    var author = null;
    var posted = null;
    var md = '';
    if (article && article.content) {
      title = article.title || title;
      author = (article.byline || '').replace(/^\s*by\s+/i, '').trim() || null;
      posted = article.publishedTime || null;
      var td = new TurndownService({
        headingStyle: 'atx', codeBlockStyle: 'fenced', bulletListMarker: '-',
      });
      td.remove(['script', 'style', 'noscript', 'iframe']);
      md = td.turndown(article.content);
    }
    if (!md.trim()) {
      var meta = document.querySelector('meta[name="description"]');
      var desc = meta ? meta.getAttribute('content') || '' : '';
      var body = document.body ? document.body.innerText : '';
      md = [desc, body].filter(Boolean).join('\n\n');
    }
    var selection = String(window.getSelection ? window.getSelection() : '').trim();
    if (selection) {
      // A selection is the reader's own emphasis: keep it at the top,
      // as a quote, above the full text.
      md = '> ' + selection.replace(/\n+/g, '\n> ') + '\n\n' + md;
    }
    return {
      url: location.href,
      title: title,
      content: md,
      author: author || location.hostname.replace(/^www\./, ''),
      posted_at: posted,
    };
  }

  var result = pdfCapture();
  if (!result && /(^|\.)(x|twitter)\.com$/.test(location.hostname)) result = tweetCapture();
  if (!result) result = pageCapture();
  if (result && result.content && result.content.length > MAX_CHARS) {
    result.content = result.content.slice(0, MAX_CHARS);
  }
  return result;
})();
