#!/usr/bin/env python3
"""Checks that the app's Terms text matches the web's TermsModal.js word for word.

The web hard-codes the Terms in frontend/src/components/TermsModal.js; the app
bundles the same text in ios/Loore/Features/Onboarding/TermsText.swift. This
compares the visible text of both (markup, quotes escaping and whitespace
normalised) and prints the first difference.

Usage: python3 ios/scripts/check_terms_text.py   (from the repo root; exit 1 on mismatch)
"""
import html
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
JS = ROOT / "frontend/src/components/TermsModal.js"
SWIFT = ROOT / "ios/Loore/Features/Onboarding/TermsText.swift"


def web_words():
    src = JS.read_text(encoding="utf-8")
    start = src.index("<h2")
    end = src.index("{error &&")
    jsx = src[start:end]
    jsx = re.sub(r"\{/\*.*?\*/\}", " ", jsx, flags=re.S)  # JSX comments
    jsx = jsx.replace('{" "}', " ")
    jsx = re.sub(r"</?(strong|em|a)\b[^>]*>", "", jsx)  # inline tags: no separator
    jsx = re.sub(r"<[^>]+>", " ", jsx)  # block tags: separator
    text = html.unescape(jsx)
    return text.split()


def app_words():
    src = SWIFT.read_text(encoding="utf-8")
    start = src.index("static let blocks")
    body = src[start:]
    literals = re.findall(r'"((?:\\.|[^"\\])*)"', body)
    words = []
    for lit in literals:
        text = lit.replace('\\"', '"').replace("\\\\", "\\")
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # markdown links
        text = text.replace("**", "").replace("*", "")
        words.extend(text.split())
    return words


def main():
    web, app = web_words(), app_words()
    for i, (w, a) in enumerate(zip(web, app)):
        if w != a:
            ctx_w = " ".join(web[max(0, i - 8): i + 8])
            ctx_a = " ".join(app[max(0, i - 8): i + 8])
            print(f"Mismatch at word {i}: web {w!r} vs app {a!r}\n  web: …{ctx_w}…\n  app: …{ctx_a}…")
            return 1
    if len(web) != len(app):
        print(f"Length differs: web {len(web)} words, app {len(app)} words")
        longer = web if len(web) > len(app) else app
        print("  extra:", " ".join(longer[min(len(web), len(app)):][:30]))
        return 1
    print(f"Terms text matches ({len(web)} words).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
