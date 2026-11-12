import React from "react";
import { Link } from "react-router-dom";

// Renders a legal document (frontend/src/legal/terms.json, privacy.json).
// The JSON files are the only copy of the text: the public pages (/terms,
// /privacy), the after-login TermsModal and the iPhone app's TermsText.swift
// (generated from terms.json by ios/scripts/check_terms_text.py) all read it.
//
// Inline text supports the subset the iPhone's Markdown parser shares:
// **bold**, *italic* and [text](url). Nothing else is interpreted.

const INLINE = /(\*\*[^*]+\*\*|\*[^*\s][^*]*\*|\[[^\]]+\]\([^)\s]+\))/;
const LINK = /^\[([^\]]+)\]\(([^)\s]+)\)$/;
// A link to loore.org itself stays on the site it is read on (staging,
// localhost), so it becomes a path.
const OWN_SITE = /^https:\/\/(www\.)?loore\.org(?=\/)/;

export function internalPath(href) {
  const path = href.replace(OWN_SITE, "");
  return path.startsWith("/") ? path : null;
}

function LegalLink({ href, newTab, children }) {
  const path = internalPath(href);
  if (path && !newTab) return <Link to={path}>{children}</Link>;
  if (path) {
    return <a href={path} target="_blank" rel="noopener noreferrer">{children}</a>;
  }
  if (href.startsWith("mailto:")) return <a href={href}>{children}</a>;
  return <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>;
}

export function renderInline(text, { newTab = false } = {}) {
  // split() with one capturing group puts the matches at the odd indices.
  return text.split(new RegExp(INLINE.source, "g")).map((part, i) => {
    if (!part) return null;
    if (i % 2 === 0) return <React.Fragment key={i}>{part}</React.Fragment>;
    if (part.startsWith("**")) return <strong key={i}>{part.slice(2, -2)}</strong>;
    const link = LINK.exec(part);
    if (link) {
      return <LegalLink key={i} href={link[2]} newTab={newTab}>{link[1]}</LegalLink>;
    }
    return <em key={i}>{part.slice(1, -1)}</em>;
  });
}

// Every bracketed capital placeholder ("[OPERATOR NAME]") still in a document.
export function findPlaceholders(doc) {
  const found = new Set();
  const visit = (value) => {
    if (typeof value === "string") {
      for (const m of value.matchAll(/\[([A-Z][A-Z0-9 /&.,'-]+)\](?!\()/g)) found.add(m[0]);
    } else if (Array.isArray(value)) {
      value.forEach(visit);
    } else if (value && typeof value === "object") {
      Object.values(value).forEach(visit);
    }
  };
  visit(doc);
  return [...found];
}

export const legalStyles = `
  .loore-legal {
    font-family: var(--sans);
    font-weight: 300;
    color: var(--text-secondary);
    line-height: 1.7;
  }
  .loore-legal strong { color: var(--text-primary); font-weight: 400; }
  .loore-legal a {
    color: var(--accent);
    text-decoration: none;
    border-bottom: 1px solid var(--accent-glow);
  }
  .loore-legal a:hover { color: var(--accent-hover); }
  .loore-legal-title {
    font-family: var(--serif);
    font-weight: 300;
    color: var(--text-primary);
    line-height: 1.2;
    font-size: clamp(2rem, 5vw, 2.8rem);
    margin-bottom: 1.5rem;
  }
  .loore-legal--modal .loore-legal-title { font-size: 1.6rem; margin-bottom: 0.5rem; }
  .loore-legal-section {
    font-family: var(--serif);
    font-weight: 400;
    font-size: 1.45rem;
    line-height: 1.3;
    color: var(--text-primary);
    margin: 2.6rem 0 0.8rem;
    scroll-margin-top: 72px;
  }
  .loore-legal--modal .loore-legal-section { font-size: 1.25rem; margin-top: 1.8rem; }
  .loore-legal-sub {
    font-family: var(--sans);
    font-weight: 400;
    font-size: 1rem;
    color: var(--text-primary);
    margin: 1.4rem 0 0.4rem;
  }
  .loore-legal p { margin: 0 0 0.9rem; }
  .loore-legal ul, .loore-legal ol { padding-left: 1.3rem; margin: 0 0 1rem; }
  .loore-legal li { margin-bottom: 0.45rem; }
  .loore-legal ul.loore-legal-plain { list-style: none; padding-left: 0.6rem; }
  .loore-legal-box {
    background: var(--bg-deep);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 1.1rem 1.3rem 0.4rem;
    margin: 0.8rem 0 1.2rem;
  }
  .loore-legal-page .loore-legal-box { background: var(--bg-card); }
  .loore-legal-box-title { color: var(--text-primary); font-weight: 400; margin-bottom: 0.6rem; }
  .loore-legal-table-wrap { overflow-x: auto; margin: 0.4rem 0 1.2rem; }
  .loore-legal table { width: 100%; border-collapse: collapse; }
  .loore-legal th {
    text-align: left; font-weight: 400; color: var(--text-primary);
    border-bottom: 1px solid var(--border); padding: 8px 10px 8px 0;
  }
  .loore-legal td {
    vertical-align: top; border-bottom: 1px solid var(--border);
    padding: 8px 10px 8px 0;
  }
  .loore-legal tr:last-child td { border-bottom: none; }
  .loore-legal hr { border: none; border-top: 1px solid var(--border); margin: 1.8rem 0; }
  .loore-legal-footnote { font-size: 0.85rem; color: var(--text-muted); }
`;

function Block({ block, newTab }) {
  const inline = (text) => renderInline(text, { newTab });
  switch (block.type) {
    case "title":
      return <h1 className="loore-legal-title">{inline(block.text)}</h1>;
    case "summaryBox":
      return (
        <div className="loore-legal-box">
          <p className="loore-legal-box-title">{inline(block.title)}</p>
          <ol>{block.items.map((item, i) => <li key={i}>{inline(item)}</li>)}</ol>
        </div>
      );
    case "quoteBox":
      return (
        <div className="loore-legal-box">
          {block.items.map((item, i) => <p key={i}>{inline(item)}</p>)}
        </div>
      );
    case "sectionTitle":
      return <h2 className="loore-legal-section" id={block.id}>{inline(block.text)}</h2>;
    case "subheading":
      return <h3 className="loore-legal-sub">{inline(block.text)}</h3>;
    case "paragraph":
      return <p>{inline(block.text)}</p>;
    case "bullets":
      return <ul>{block.items.map((item, i) => <li key={i}>{inline(item)}</li>)}</ul>;
    case "plainList":
      return (
        <ul className="loore-legal-plain">
          {block.items.map((item, i) => <li key={i}>{inline(item)}</li>)}
        </ul>
      );
    case "table":
      return (
        <div className="loore-legal-table-wrap">
          <table>
            <thead>
              <tr>{block.header.map((cell, i) => <th key={i}>{inline(cell)}</th>)}</tr>
            </thead>
            <tbody>
              {block.rows.map((row, r) => (
                <tr key={r}>{row.map((cell, c) => <td key={c}>{inline(cell)}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    case "rule":
      return <hr />;
    case "footnote":
      return <p className="loore-legal-footnote">{inline(block.text)}</p>;
    default:
      return null;
  }
}

// `variant`: "page" for /terms and /privacy, "modal" for the acceptance
// dialog. In the modal every link opens a new tab, so the dialog stays.
export default function LegalDocument({ doc, variant = "page" }) {
  const newTab = variant === "modal";
  return (
    <div className={`loore-legal loore-legal--${variant}`}>
      <style>{legalStyles}</style>
      {doc.blocks.map((block, i) => <Block key={i} block={block} newTab={newTab} />)}
    </div>
  );
}
