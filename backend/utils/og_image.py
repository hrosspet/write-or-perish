"""Per-article social cards (og:image / twitter:image), 1200×630 PNG.

Rendered server-side with Pillow in Loore's own design system — warm
near-black ground, Cormorant Garamond title, Outfit byline, and the
amber heartbeat as the one signature element: a full-width baseline
with a single pulse at the text margin, the logo's waveform living
under the words.

Composition is medium-aware: X overlays its own title pill in the
image's bottom-left corner, so everything meaningful sits in the upper
two thirds and the bottom ~110px stays quiet ground.

Fonts are the OFL-licensed variable TTFs vendored in
backend/assets/fonts/ (licenses alongside).
"""
import io
import os

from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 630
MARGIN = 90

BG_TOP = (20, 17, 13)        # faint warm lift at the top
BG_BOTTOM = (13, 12, 10)     # settles into the app's --bg-deep
PARCHMENT = (237, 232, 221)  # --text-primary
MUTED = (168, 159, 143)      # --text-secondary
AMBER = (196, 149, 106)      # --accent
HAIRLINE = (41, 37, 32)      # quiet rule the pulse interrupts

_FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "assets", "fonts")
_SERIF = os.path.join(_FONT_DIR, "CormorantGaramond[wght].ttf")
_SANS = os.path.join(_FONT_DIR, "Outfit[wght].ttf")

_fonts = {}


def _font(path, size, variation):
    key = (path, size, variation)
    if key not in _fonts:
        f = ImageFont.truetype(path, size)
        f.set_variation_by_name(variation)
        _fonts[key] = f
    return _fonts[key]


def _background():
    """Vertical gradient, barely perceptible — flat black reads dead on
    X's dark UI; this keeps the ground warm without becoming a look."""
    col = Image.new("RGB", (1, H))
    for y in range(H):
        t = y / (H - 1)
        col.putpixel((0, y), tuple(
            round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)))
    return col.resize((W, H))


def _pulse(draw, x, y, scale=1.0, width=3):
    """The logo's heartbeat: flat — spike — deep dip — recover — flat.
    Returns the x where the flat line resumes."""
    pts = [(0, 0), (26, 0), (38, -24), (52, 34), (64, -8), (74, 0),
           (108, 0)]
    scaled = [(x + px * scale, y + py * scale) for px, py in pts]
    draw.line(scaled, fill=AMBER, width=width, joint="curve")
    return x + 108 * scale


def _spaced_text(draw, xy, text, font, fill, tracking):
    """Letterspaced caps for the eyebrow — Outfit tracked wide, the way
    the app sets its small labels."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking
    return x - tracking


def _wrap(draw, text, font, max_width, max_lines):
    """Greedy word wrap; the last permitted line is ellipsized when the
    text overflows."""
    words = text.split()
    lines, current = [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
            continue
        lines.append(current)
        current = word
        if len(lines) == max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and " ".join(lines) + "" != text \
            and " ".join(words) != " ".join(lines):
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_width:
            last = last[:-1].rstrip()
        lines[-1] = last + "…"
    return lines


def _eyebrow(draw):
    end = _pulse(draw, MARGIN, 92, scale=0.62, width=3)
    label = _font(_SANS, 26, "Medium")
    _spaced_text(draw, (end + 26, 78), "LOORE", label, MUTED, 10)


def _heartbeat_rule(draw, y):
    """The signature: a hairline across the full bleed, alive for one
    beat at the text margin."""
    draw.line([(0, y), (MARGIN, y)], fill=HAIRLINE, width=2)
    end = _pulse(draw, MARGIN, y, scale=1.0, width=3)
    draw.line([(end, y), (W, y)], fill=HAIRLINE, width=2)


def _render(title_text, byline_text, title_max_lines=3):
    img = _background()
    draw = ImageDraw.Draw(img)
    _eyebrow(draw)

    size = 84
    title_font = _font(_SERIF, size, "Light")
    lines = _wrap(draw, title_text, title_font, W - 2 * MARGIN,
                  title_max_lines)
    if len(lines) > 2:
        size = 68
        title_font = _font(_SERIF, size, "Light")
        lines = _wrap(draw, title_text, title_font, W - 2 * MARGIN,
                      title_max_lines)

    y = 196
    for line in lines:
        draw.text((MARGIN, y), line, font=title_font, fill=PARCHMENT)
        y += round(size * 1.14)

    y += 34
    byline_font = _font(_SANS, 30, "Light")
    draw.text((MARGIN, y), byline_text, font=byline_font, fill=MUTED)

    # Below the byline but above X's title-pill zone; never closer to the
    # byline than the pulse's upstroke needs.
    rule_y = min(y + 92, H - 90)
    rule_y = max(rule_y, y + 78)
    _heartbeat_rule(draw, rule_y)

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def render_article_card(title, author, published=None):
    when = published.strftime("%-d %B %Y") if published else None
    byline = f"@{author} · {when}" if when else f"@{author}"
    return _render(title, byline)


def render_profile_card(username, description=None):
    byline = (description or "").strip() or "Published writing on Loore"
    if len(byline) > 90:
        byline = byline[:90].rsplit(" ", 1)[0] + "…"
    return _render(f"@{username}", byline)
