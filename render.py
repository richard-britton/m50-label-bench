"""Server-side label rendering for prints that come from Home Assistant.

Text is auto-fitted to the label, with emoji drawn from a monochrome emoji font
(Noto Emoji, falling back to Symbola, then DejaVu). The page renders its own
labels in the browser and sends finished bitmaps, so this is only used when
someone prints plain text (for example from an automation).
"""
import glob
import os
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont, ImageOps

try:
    from fontTools.ttLib import TTFont
except ImportError:  # coverage checks degrade to "first font wins"
    TTFont = None

DPMM = 8  # 203 dpi is 8 dots per millimetre
FONT_DIR = os.environ.get("FONT_DIR", "/app/fonts")

_TEXT_CANDIDATES = [
    (f"{FONT_DIR}/Archivo.ttf", {"Weight": 800, "Width": 75}),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf", None),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", None),
]
_EMOJI_CANDIDATES = [(f"{FONT_DIR}/NotoEmoji.ttf", {"Weight": 600})]
_EMOJI_CANDIDATES += [(p, None) for p in sorted(glob.glob("/usr/share/fonts/**/Symbola*.ttf", recursive=True))]
_EMOJI_CANDIDATES += [("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", None)]

# Characters that only modify the previous emoji. Without a shaping engine they would draw as boxes.
_SKIP = {0xFE0E, 0xFE0F, 0x200D, *range(0x1F3FB, 0x1F400)}


def _is_emojiish(cp):
    return cp >= 0x1F000 or 0x2600 <= cp <= 0x27BF or 0x2B00 <= cp <= 0x2BFF or 0x2190 <= cp <= 0x21FF


@lru_cache(maxsize=None)
def _cmap(path):
    if TTFont is None:
        return None
    try:
        return frozenset(TTFont(path, lazy=True, fontNumber=0)["cmap"].getBestCmap().keys())
    except Exception:
        return None


def _available(candidates):
    return [(p, axes) for p, axes in candidates if os.path.exists(p)]


TEXT_FONTS = _available(_TEXT_CANDIDATES)
EMOJI_FONTS = _available(_EMOJI_CANDIDATES)


@lru_cache(maxsize=256)
def _font(path, size, axes_key):
    f = ImageFont.truetype(path, size)
    if axes_key:
        wanted = dict(axes_key)
        try:
            vals = []
            for ax in f.get_variation_axes():
                name = ax.get("name", b"")
                name = name.decode() if isinstance(name, bytes) else str(name)
                lo, hi, default = ax.get("minimum"), ax.get("maximum"), ax.get("default")
                v = next((val for key, val in wanted.items() if key.lower() in name.lower()), default)
                vals.append(max(lo, min(hi, v)))
            f.set_variation_by_axes(vals)
        except Exception:
            pass  # not a variable font, or FreeType without variation support
    return f


def _pick(cp, prefer_emoji):
    chains = (EMOJI_FONTS + TEXT_FONTS) if prefer_emoji else (TEXT_FONTS + EMOJI_FONTS)
    for path, axes in chains:
        cm = _cmap(path)
        if cm is None or cp in cm:
            return path, axes
    return chains[0] if chains else (None, None)


def _runs(line):
    """Split a line into (text, font path, axes) runs so each character uses a font that has it."""
    runs = []
    chars = list(line)
    for i, ch in enumerate(chars):
        cp = ord(ch)
        if cp in _SKIP:
            continue
        nxt = ord(chars[i + 1]) if i + 1 < len(chars) else 0
        path, axes = _pick(cp, _is_emojiish(cp) or nxt == 0xFE0F)
        key = (path, tuple(sorted(axes.items())) if axes else None)
        if runs and runs[-1][1] == key:
            runs[-1][0] += ch
        else:
            runs.append([ch, key])
    return [(t, k[0], k[1]) for t, k in runs]


def _measure(lines_runs, size, spacing):
    width = asc = desc = 0
    for runs in lines_runs:
        w = 0
        for text, path, axes in runs:
            f = _font(path, size, axes)
            w += f.getlength(text)
            x0, y0, x1, y1 = f.getbbox(text, anchor="ls")
            asc, desc = max(asc, -y0), max(desc, y1)
        width = max(width, w)
    if not asc and not desc:
        asc = size * 0.7
    line_h = size * spacing
    return width, asc, desc, asc + desc + (len(lines_runs) - 1) * line_h, line_h


def render_text(text, length_mm=30, head_dots=112, margin_mm=1.5, align="center", spacing=1.05, invert=False):
    """Return a 1-bit PIL image (width = label length in dots, height = head dots) with the text fitted to it."""
    if not TEXT_FONTS:
        raise RuntimeError("No fonts available for rendering text")
    W, H = max(16, int(round(float(length_mm) * DPMM))), int(head_dots)
    margin = int(round(float(margin_mm) * DPMM))
    box_w, box_h = W - 2 * margin, H - 2 * margin
    lines = str(text or "").replace("\r", "").split("\n")
    while len(lines) > 1 and not lines[-1].strip():
        lines.pop()
    lines_runs = [_runs(ln) for ln in lines]

    img = Image.new("L", (W, H), 255)
    if any(lines_runs):
        lo, hi = 4, int(box_h * 2.4)
        while lo < hi:  # largest size that fits both ways
            mid = (lo + hi + 1) // 2
            w, _, _, h, _ = _measure(lines_runs, mid, spacing)
            if w <= box_w and h <= box_h:
                lo = mid
            else:
                hi = mid - 1
        size = lo
        _, asc, _, block_h, line_h = _measure(lines_runs, size, spacing)
        draw = ImageDraw.Draw(img)
        y = margin + (box_h - block_h) / 2 + asc
        for runs in lines_runs:
            line_w = sum(_font(p, size, a).getlength(t) for t, p, a in runs)
            x = margin if align == "left" else (W - margin - line_w if align == "right" else (W - line_w) / 2)
            for t, p, a in runs:
                f = _font(p, size, a)
                draw.text((x, round(y)), t, font=f, fill=0, anchor="ls")
                x += f.getlength(t)
            y += line_h
    if invert:
        img = ImageOps.invert(img)
    return img.point(lambda v: 0 if v < 128 else 255, mode="1")


def prepare_image(img, head_dots=112, length_mm=None, dither=True):
    """Fit any image to the printhead height and return a 1-bit image ready for printing."""
    img = img.convert("RGBA")
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    img = Image.alpha_composite(bg, img).convert("L")
    if img.height != head_dots:
        w = max(1, round(img.width * head_dots / img.height))
        img = img.resize((w, head_dots), Image.LANCZOS)
    if length_mm:
        target = int(round(float(length_mm) * DPMM))
        if img.width < target:
            canvas = Image.new("L", (target, head_dots), 255)
            canvas.paste(img, ((target - img.width) // 2, 0))
            img = canvas
    two_tone = set(img.getdata()) <= {0, 255}
    if two_tone or not dither:
        return img.point(lambda v: 0 if v < 128 else 255, mode="1")
    return img.convert("1")  # Floyd-Steinberg dithering for photos and colour emoji
