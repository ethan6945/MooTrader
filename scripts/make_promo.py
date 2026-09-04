"""The 28-second product film, rendered frame by frame.

  .venv/bin/python scripts/make_promo.py docs/screenshots /tmp/frames
  ffmpeg -y -framerate 30 -i /tmp/frames/f%05d.png -c:v libx264 -preset slow \
         -crf 18 -pix_fmt yuv420p -movflags +faststart docs/promo/promo.mp4

The two modal shots are cropped to the modal first (v-parameters.png,
v-settings.png): the full-window versions carry a dimmed dashboard behind the
sheet, which at video scale reads as mud rather than as depth.


Keynote grammar, deliberately: pure black, one idea per shot, type that enters
by fading up a couple of dozen pixels on an ease-out and then simply sits there.
No wipes, no slides, no motion for its own sake — the restraint IS the style,
and the fastest way to lose it is to animate two things at once.
"""
import math, os, sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H, FPS = 1920, 1080, 30
BG = (0, 0, 0)
FG = (245, 245, 247)          # Apple's off-white; pure #fff glares on black
DIM = (134, 134, 139)         # their secondary grey
ACCENT = (88, 166, 255)       # the panel's own blue
GREEN = (48, 209, 88)

# PingFang SC lives inside a downloadable font asset on modern macOS, so it is
# found rather than hardcoded — the path contains a hash that differs per
# machine and changes when the asset updates.
def _pingfang() -> Path:
    import subprocess
    out = subprocess.run(["fc-list"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "PingFang SC" in line:
            return Path(line.split(":")[0])
    raise SystemExit("PingFang SC not found — this renderer is macOS-only")

PF = _pingfang()
SHOTS = Path(sys.argv[1])
OUT = Path(sys.argv[2]); OUT.mkdir(parents=True, exist_ok=True)

_fc = {}
def font(size, weight="thin"):
    idx = {"ultralight": 23, "thin": 19, "light": 15,
           "regular": 3, "medium": 7, "semibold": 11}[weight]
    k = (size, idx)
    if k not in _fc:
        _fc[k] = ImageFont.truetype(str(PF), size, index=idx)
    return _fc[k]


def ease_out(t):      return 1 - (1 - t) ** 3
def ease_in_out(t):   return 4*t*t*t if t < .5 else 1 - (-2*t + 2)**3 / 2


def text_layer(lines):
    """Pre-render a shot's type once; frames only move and fade it."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    total = 0
    metrics = []
    for txt, size, weight, colour, track, gap in lines:
        f = font(size, weight)
        w = sum(d.textlength(c, font=f) + track for c in txt) - track if txt else 0
        metrics.append((txt, f, w, colour, track, size, gap))
        total += size * 1.25 + gap
    y = (H - total) / 2
    for txt, f, w, colour, track, size, gap in metrics:
        x = (W - w) / 2
        for c in txt:                      # manual tracking; PIL has none
            d.text((x, y), c, font=f, fill=colour + (255,))
            x += d.textlength(c, font=f) + track
        y += size * 1.25 + gap
    return img


def shot_card(img, lines, out_frames, fade_in=.9, hold=None):
    """A type-only shot on black."""
    layer = text_layer(lines)
    n = out_frames
    fi = int(fade_in * FPS)
    fo = int(.55 * FPS)
    for i in range(n):
        base = Image.new("RGB", (W, H), BG)
        if i < fi:
            t = ease_out(i / fi); a, dy = t, int((1 - t) * 26)
        elif i > n - fo:
            t = (n - i) / fo; a, dy = max(0.0, t), 0
        else:
            a, dy = 1.0, 0
        if a > 0:
            l = layer if dy == 0 else layer.transform(
                (W, H), Image.AFFINE, (1, 0, 0, 0, 1, -dy), resample=Image.BILINEAR)
            if a < 1:
                l = l.copy(); l.putalpha(l.getchannel("A").point(lambda v: int(v * a)))
            base.paste(l, (0, 0), l)
        yield base


def rounded(im, r=16):
    m = Image.new("L", im.size, 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, im.size[0]-1, im.size[1]-1], r, fill=255)
    out = im.convert("RGBA"); out.putalpha(m); return out


def shot_image(png, caption, n, zoom_from=1.06):
    """A product shot that settles: it drifts to rest rather than arriving."""
    src = Image.open(SHOTS / png).convert("RGB")
    target_w = int(W * .74)
    scale = target_w / src.width
    base_w, base_h = target_w, int(src.height * scale)
    if base_h > H * .70:
        base_h = int(H * .70); base_w = int(src.width * base_h / src.height)
    cap = text_layer([(caption, 40, "light", DIM, 3, 0)]) if caption else None
    fi, fo = int(1.0 * FPS), int(.55 * FPS)
    for i in range(n):
        base = Image.new("RGB", (W, H), BG)
        p = ease_in_out(min(1.0, i / (n * .85)))
        z = zoom_from + (1.0 - zoom_from) * p
        w, h = int(base_w * z), int(base_h * z)
        im = rounded(src.resize((w, h), Image.LANCZOS))
        a = ease_out(i / fi) if i < fi else ((n - i) / fo if i > n - fo else 1.0)
        a = max(0.0, min(1.0, a))
        x, y = (W - w) // 2, int(H * .40) - h // 2 + int((1 - a) * 14)
        # A soft plate under the shot, so it sits on the black instead of
        # floating on it.
        glow = Image.new("RGBA", (w + 120, h + 120), (0, 0, 0, 0))
        ImageDraw.Draw(glow).rounded_rectangle([60, 60, w + 59, h + 59], 22,
                                               fill=(88, 166, 255, int(26 * a)))
        glow = glow.filter(ImageFilter.GaussianBlur(38))
        base.paste(glow, (x - 60, y - 60), glow)
        if a < 1:
            im = im.copy(); im.putalpha(im.getchannel("A").point(lambda v: int(v * a)))
        base.paste(im, (x, y), im)
        if cap is not None:
            c = cap.transform((W, H), Image.AFFINE, (1, 0, 0, 0, 1, -(int(H * .34))),
                              resample=Image.BILINEAR)
            c = c.copy(); c.putalpha(c.getchannel("A").point(lambda v: int(v * a)))
            base.paste(c, (0, 0), c)
        yield base


S = lambda sec: int(sec * FPS)
STORY = [
    ("card", S(2.9), [("MooTrader", 132, "ultralight", FG, 14, 26),
                      ("美股波段交易，自动执行", 40, "light", DIM, 8, 0)]),
    ("card", S(3.2), [("规则决定进出", 96, "thin", FG, 10, 18),
                      ("AI 只做注释，不做决定", 40, "light", DIM, 6, 0)]),
    ("img",  S(4.6), "01-dashboard.png", "预算、持仓、板块、每一笔平仓的理由"),
    ("img",  S(4.2), "v-parameters.png", "每个参数都能改，改完知道何时生效"),
    ("card", S(3.3), [("30", 220, "ultralight", FG, 6, 4),
                      ("天免费试用", 52, "light", FG, 10, 22),
                      ("功能一点不留", 34, "light", DIM, 6, 0)]),
    ("card", S(3.6), [("到期后依然运行", 84, "thin", FG, 10, 18),
                      ("只是不再下单 —— 它仍然告诉你，它本来会怎么做", 38, "light", DIM, 4, 0)]),
    ("card", S(3.2), [("永久授权", 60, "light", DIM, 12, 16),
                      ("USD 30", 160, "ultralight", FG, 8, 18),
                      ("一次付清，没有订阅", 34, "light", DIM, 6, 0)]),
    ("card", S(3.4), [("MooTrader", 92, "ultralight", FG, 12, 30),
                      ("WhatsApp  @ethan45", 42, "light", ACCENT, 6, 0)]),
]

idx = 0
for shot in STORY:
    gen = (shot_card(None, shot[2], shot[1]) if shot[0] == "card"
           else shot_image(shot[2], shot[3], shot[1]))
    for fr in gen:
        fr.save(OUT / f"f{idx:05d}.png", compress_level=1)
        idx += 1
print(f"{idx} frames  ({idx/FPS:.1f}s)")
