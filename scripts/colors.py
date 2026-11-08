"""Approximate garment colour from a photo.

analyze(img) -> {"name", "hex", "family", "tone", "secondary": [{name, hex, share}]}

- Pixels: the non-transparent ones of a cut-out (background removed), otherwise the
  central 60 % of the photo.
- Dominant colours: PIL median-cut quantisation into 5 clusters.
- Each cluster is matched to the nearest named colour in PALETTE by CIELAB distance.
- family: нейтральный / землистый / тёплый / холодный — used for outfit colour rules
  (see wardrobe/AI_GUIDE.md); tone: светлый / средний / тёмный by L*.
"""
from __future__ import annotations

# name, hex, family
PALETTE = [
    ("белый", "#f5f5f2", "нейтральный"), ("молочный", "#efe6d2", "нейтральный"),
    ("светло-серый", "#c8c8c6", "нейтральный"), ("серо-бежевый", "#b9b2a5", "нейтральный"), ("серый", "#8d9096", "нейтральный"),
    ("графит", "#3a3d42", "нейтральный"), ("чёрный", "#151515", "нейтральный"),
    ("тёмно-синий", "#1f2a44", "нейтральный"),
    ("бежевый", "#d8c3a5", "землистый"), ("песочный", "#c9b38a", "землистый"), ("кэмел", "#b98b5e", "землистый"),
    ("хаки", "#8a7f55", "землистый"), ("оливковый", "#5b5f2f", "землистый"),
    ("коричневый", "#6b4a32", "землистый"), ("шоколадный", "#4a2c1d", "землистый"),
    ("синий", "#2c4f8a", "холодный"), ("голубой", "#8fb3d9", "холодный"), ("деним", "#3b5a7d", "холодный"),
    ("бирюзовый", "#2a9d8f", "холодный"), ("зелёный", "#2f6b3f", "холодный"), ("мятный", "#a8d5ba", "холодный"),
    ("розовый", "#e7a6b8", "холодный"), ("фиолетовый", "#5e3c8c", "холодный"), ("лавандовый", "#b9a7d6", "холодный"),
    ("горчичный", "#c9a227", "тёплый"), ("жёлтый", "#e2c044", "тёплый"), ("оранжевый", "#e07b39", "тёплый"),
    ("терракотовый", "#b5543a", "тёплый"), ("красный", "#b3261e", "тёплый"), ("бордовый", "#6d1f2a", "тёплый"),
]
ALIASES = {"черный": "чёрный", "темно-синий": "тёмно-синий", "зеленый": "зелёный", "желтый": "жёлтый",
           "экрю": "молочный", "кремовый": "молочный", "терракота": "терракотовый", "navy": "тёмно-синий"}


def _hex_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _lab(rgb):
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t):
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


_PAL_LAB = [(n, h, fam, _lab(_hex_rgb(h))) for n, h, fam in PALETTE]


def nearest(rgb):
    lab = _lab(rgb)
    name, hx, fam, _ = min(_PAL_LAB, key=lambda p: sum((a - b) ** 2 for a, b in zip(lab, p[3])))
    return name, hx, fam


def by_name(name: str):
    """Palette entry for a colour typed by the user (or None)."""
    key = ALIASES.get(name.strip().lower(), name.strip().lower())
    return next(((n, h, f) for n, h, f in PALETTE if n == key), None)


def tone(rgb) -> str:
    L = _lab(rgb)[0]
    return "светлый" if L > 70 else "тёмный" if L < 35 else "средний"


def analyze(img) -> dict:
    from PIL import Image
    im = img.convert("RGBA")
    im.thumbnail((160, 160))
    has_alpha = im.getchannel("A").getextrema()[0] < 250
    if not has_alpha:
        w, h = im.size
        im = im.crop((int(w * .2), int(h * .2), int(w * .8), int(h * .8)))
    px = [p[:3] for p in im.getdata() if p[3] > 200]
    if not px:
        return {}
    flat = Image.new("RGB", (len(px), 1))
    flat.putdata(px)
    q = flat.quantize(colors=5, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette()
    counts = sorted(q.getcolors(), reverse=True)
    total = sum(c for c, _ in counts)
    merged: dict[str, dict] = {}
    for c, idx in counts:
        rgb = tuple(pal[idx * 3: idx * 3 + 3])
        name, hx, fam = nearest(rgb)
        m = merged.setdefault(name, {"name": name, "hex": hx, "family": fam, "share": 0, "rgb": rgb})
        m["share"] += c / total
    ranked = sorted(merged.values(), key=lambda m: -m["share"])
    main = ranked[0]
    return {
        "name": main["name"], "hex": main["hex"], "family": main["family"], "tone": tone(main["rgb"]),
        "secondary": [{"name": m["name"], "hex": m["hex"], "share": round(m["share"], 2)}
                      for m in ranked[1:] if m["share"] >= 0.12],
        "auto": True,
    }


def from_name(name: str, current: dict | None = None) -> dict:
    """User-typed colour name wins over the automatic one; keep auto secondary colours."""
    entry = by_name(name)
    color = dict(current or {})
    color["name"] = name.strip()
    if entry:
        color.update(hex=entry[1], family=entry[2], tone=tone(_hex_rgb(entry[1])))
    color["auto"] = False
    return color
