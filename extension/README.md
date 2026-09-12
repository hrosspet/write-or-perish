# Loore Clipper

A small Chrome extension (Manifest V3) that saves the current tab into your
Loore **references** with one key press and closes the tab. It exists for
the moment when a browser holds far too many tabs and each one deserves a
second-long decision: keep it in Loore, or let it go.

What lands in Loore is a *reference*, not your writing. It is searchable
and quotable in conversations, and it never enters your profile.

## What it captures

- **Articles and pages**: Mozilla Readability extracts the main text, Turndown
  turns it into Markdown (headings, lists, links, code survive; no HTML is
  stored). If you have text selected on the page, it is kept at the top as a
  quote. Title, byline and publish date come along when the page exposes them.
- **Tweets** (`x.com/…/status/…`): text, author, timestamp, quoted tweet,
  link card, photo alt text, expanded links. They are stored as
  `twitter_bookmark` items keyed by tweet id, so a clipped tweet and the same
  tweet arriving through the bookmark sync are one row.
- **PDFs** opened in Chrome's viewer: the link only, titled from the file
  name (or the PDF's own title when Chrome shows one). The text is not
  extracted; the reference says so.
- Content is capped at 100,000 characters, the same bound as a Loore entry.

## Install (unpacked)

1. Open `chrome://extensions`, turn on **Developer mode**, click **Load
   unpacked**, and choose this `extension/` folder.
2. In Loore, open **Import** and under **Chrome clipper** click **Create
   token**. Copy it; it is shown once.
3. Click the extension's **Options** (or the puzzle-piece menu), pick the
   Loore instance, paste the token, **Test connection**.
4. Optional: change the shortcuts at `chrome://extensions/shortcuts`.

## Use

- `Alt+Shift+L`: clip the current tab and close it.
- `Alt+Shift+K`: clip and keep the tab open.
- `Cmd+W` as usual to discard a tab without clipping.

The toolbar badge shows `✓` (saved or updated), `=` (already saved earlier)
or `!` (failed; open the popup to read why). Pressing twice on the same page
is harmless: the server dedupes by canonical URL or tweet id. If the new
capture has more text than the stored copy (a long post the X bookmark sync
stored truncated, say), the stored text is replaced with the fuller one.

## Security model

- The token is scoped to `external:write`. It can add references to your
  account and nothing else; it cannot read a single entry. Revoke it any time
  on the Import page.
- Only the token's hash is stored server-side.
- The extension asks for `activeTab`, not `<all_urls>`: it can read a page
  only at the moment you press the shortcut or click its button.
- Host access is limited to loore.org, staging.loore.org and localhost:3001.
- The token lives in `chrome.storage.local` on this machine (not synced).

## Known limits

- X keeps `t.co` redirects in link hrefs and shows a possibly elided URL as
  text; the clip records the text as shown.
- Pages Chrome forbids scripts on (`chrome://`, the Web Store) cannot be
  captured.
- The `web_clip` author is the byline when Readability finds one, else the
  site's hostname.
