"""Add a wardrobe item (or extra photos) from a GitHub issue form.

Used by .github/workflows/add-item.yml. Reads the issue body from $ISSUE_BODY,
downloads attached photos, removes the background with rembg, saves WebP files
to wardrobe/photos/ and updates wardrobe/wardrobe.json.

Local use (cut out one photo):  python scripts/add_item.py cutout in.jpg out.webp
"""
import json
import os
import re
import sys
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "wardrobe" / "wardrobe.json"
PHOTOS = ROOT / "wardrobe" / "photos"
MAX_SIDE = 1200

WARMTH_TEMP = {1: [20, 40], 2: [13, 22], 3: [8, 18], 4: [-2, 12], 5: [-20, 5]}
COLOR_HEX = {"белый": "#f5f5f2", "чёрный": "#151515", "черный": "#151515", "серый": "#8d9096", "графит": "#3a3d42",
             "тёмно-синий": "#1f2a44", "темно-синий": "#1f2a44", "синий": "#2c4f8a", "голубой": "#8fb3d9",
             "бежевый": "#d8c3a5", "экрю": "#efe6d2", "молочный": "#f1ead9", "коричневый": "#6b4a32",
             "шоколадный": "#4a2c1d", "хаки": "#8a7f55", "оливковый": "#5b5f2f", "зелёный": "#2f6b3f",
             "зеленый": "#2f6b3f", "бордовый": "#6d1f2a", "красный": "#b3261e", "терракотовый": "#b5543a",
             "розовый": "#e7a6b8", "жёлтый": "#e2c044", "желтый": "#e2c044", "фиолетовый": "#5e3c8c", "деним": "#3b5a7d"}
PREFIX = {"Верх": "top", "Низ": "bottom", "Верхняя одежда": "outer", "Обувь": "shoes", "Аксессуары": "acc"}

_session = None


def rembg_session():
    global _session
    if _session is None:
        from rembg import new_session
        _session = new_session("isnet-general-use")
    return _session


def cutout(img: Image.Image) -> Image.Image:
    """Remove background and crop to the object with a small margin."""
    from rembg import remove
    out = remove(img, session=rembg_session(), post_process_mask=True)
    bbox = out.getchannel("A").point(lambda a: 255 if a > 20 else 0).getbbox()
    if bbox:
        out = out.crop(bbox)
    pad = int(max(out.size) * 0.06)
    canvas = Image.new("RGBA", (out.width + 2 * pad, out.height + 2 * pad), (0, 0, 0, 0))
    canvas.alpha_composite(out, (pad, pad))
    return canvas


def load(src) -> Image.Image:
    img = Image.open(src)
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((2000, 2000))
    return img


def save(img: Image.Image, path: Path):
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    img.save(path, "WEBP", quality=82, method=6)


def dominant_hex(img: Image.Image) -> str:
    """Average colour of the visible (non-transparent) pixels."""
    small = img.convert("RGBA").copy()
    small.thumbnail((80, 80))
    px = [p for p in small.getdata() if p[3] > 200]
    if not px:
        return "#999999"
    r, g, b = (sum(p[i] for p in px) // len(px) for i in range(3))
    return f"#{r:02x}{g:02x}{b:02x}"


def parse_form(body: str) -> dict:
    """GitHub issue forms render as '### Label\\n\\nvalue' blocks."""
    fields = {}
    for block in re.split(r"^### ", body, flags=re.M)[1:]:
        label, _, value = block.partition("\n")
        value = value.strip()
        fields[label.strip()] = "" if value == "_No response_" else value
    return fields


def image_urls(text: str) -> list[str]:
    urls = re.findall(r"!\[[^\]]*\]\((https://[^)\s]+)\)", text)
    urls += re.findall(r'<img[^>]+src="(https://[^"]+)"', text)
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def checked(value: str) -> list[str]:
    """Checked options of an issue-form checkbox block ('- [x] Label')."""
    return [m.strip() for m in re.findall(r"^- \[[xX]\] (.+)$", value or "", flags=re.M)]


CONDITION = {"Отличное": "отличное", "Хорошее": "хорошее", "Есть дефекты": "есть дефекты", "Только для дома": "только дом/спорт"}


def apply_condition(item: dict, f: dict, is_new: bool):
    """Condition, flaws and where-to-wear; for existing items only fields the user filled change."""
    cond = f.get("Состояние", "")
    for key, val in CONDITION.items():
        if cond.startswith(key):
            item["condition"] = val
    flaws = [x.lower() for x in checked(f.get("Дефекты", ""))]
    if flaws:
        item["flaws"] = sorted(set(item.get("flaws", [])) | set(flaws))
    notes = f.get("Подробнее о дефектах", "").strip()
    if notes:
        item["flaw_notes"] = notes
    occ = checked(f.get("Где можно носить", ""))
    if occ:
        item["occasions"] = occ
    elif is_new and item.get("condition") == "только дом/спорт":
        item["occasions"] = ["Дом", "Спорт"]


def next_id(items, prefix):
    nums = [int(m.group(1)) for i in items if (m := re.fullmatch(prefix + r"-(\d+)", i["id"]))]
    return f"{prefix}-{max(nums, default=0) + 1:03d}"


def main():
    body = os.environ["ISSUE_BODY"]
    token = os.environ.get("GITHUB_TOKEN", "")
    f = parse_form(body)
    data = json.loads(DATA.read_text(encoding="utf-8"))
    items = data["items"]

    urls = image_urls(f.get("Фото", ""))
    existing = f.get("Добавить фото к существующей вещи (id)", "").strip()
    if existing:
        item = next((i for i in items if i["id"] == existing), None)
        if not item:
            raise SystemExit(f"Вещь с id «{existing}» не найдена.")
        apply_condition(item, f, is_new=False)
    else:
        if not urls:
            raise SystemExit("Не нашёл ни одного фото. Прикрепи фото в поле «Фото».")
        category = f.get("Категория") or "Верх"
        warmth = int((f.get("Насколько тёплая") or "2")[0])
        protection = f.get("Защита от погоды", "")
        layer = {"Верхняя одежда": "верхний", "Низ": "база", "Обувь": "обувь", "Аксессуары": "аксессуар"}.get(
            category, "база" if warmth <= 1 else "средний")
        item = {
            "id": next_id(items, PREFIX.get(category, "item")),
            "name": f.get("Название") or "Новая вещь",
            "brand": f.get("Бренд") or None,
            "category": category,
            "color": {"name": f.get("Цвет") or "", "hex": COLOR_HEX.get((f.get("Цвет") or "").strip().lower())},
            "material": f.get("Состав") or None,
            "size": f.get("Размер") or None,
            "warmth": warmth,
            "layer": layer,
            "temp": WARMTH_TEMP[warmth],
            "windproof": "[x] Держит ветер" in protection,
            "waterproof": "[x] Непромокаемая" in protection,
            "link": f.get("Ссылка на товар (WB и т.п.)") or None,
            "notes": f.get("Заметки") or None,
            "photos": [],
        }
        item = {k: v for k, v in item.items() if v is not None}
        apply_condition(item, f, is_new=True)
        items.append(item)

    mode = f.get("Удалить фон", "На всех фото")
    PHOTOS.mkdir(parents=True, exist_ok=True)
    start = len(item["photos"])
    for n, url in enumerate(urls):
        headers = {"Authorization": f"token {token}"} if token and "github" in url else {}
        r = requests.get(url, headers=headers, timeout=60)
        r.raise_for_status()
        img = load(BytesIO(r.content))
        remove = mode == "На всех фото" or (mode.startswith("Только") and start + n == 0)
        out = cutout(img) if remove else img
        name = f"{item['id']}-{start + n + 1}.webp"
        save(out, PHOTOS / name)
        item["photos"].append(f"wardrobe/photos/{name}")
        if start + n == 0 and not item.get("color", {}).get("hex"):
            item.setdefault("color", {})["hex"] = dominant_hex(out)

    DATA.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    extra = ""
    if item.get("condition"):
        extra += f"\nСостояние: {item['condition']}."
    if item.get("flaws"):
        extra += f"\nДефекты: {', '.join(item['flaws'])}."
    if item.get("occasions"):
        extra += f"\nГде носить: {', '.join(item['occasions'])}."
    msg = (f"Готово: «{item['name']}» (`{item['id']}`), фото добавлено: {len(urls)}.{extra}\n\n"
           f"Смотри на сайте через 1–2 минуты: https://ismoil77.github.io/project/#items")
    Path(os.environ.get("RESULT_FILE", "result.md")).write_text(msg, encoding="utf-8")
    print(msg)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "cutout":
        save(cutout(load(sys.argv[2])), Path(sys.argv[3]))
    else:
        main()
