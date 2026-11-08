"""Wardrobe bot: add/update/archive items from GitHub issue forms.

Used by .github/workflows/add-item.yml. Reads the issue body from $ISSUE_BODY
and the form type from $FORM ("add" or "archive").

add     — new item or update of an existing one (photos, condition, flaws, purchase date…).
          Photos are downloaded, background removed with rembg, saved as WebP.
archive — move an item to the archive or bring it back.

Local helpers:
  python scripts/add_item.py cutout in.jpg out.webp     # remove background from one photo
  python scripts/add_item.py purchased "осень 2024"     # test the purchase-date parser
Data format: wardrobe/SCHEMA.md
"""
import datetime as dt
import json
import os
import re
import sys
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import colors  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "wardrobe" / "wardrobe.json"
PHOTOS = ROOT / "wardrobe" / "photos"
MAX_SIDE = 1200
TODAY = dt.date.today()

# "Сезон и плотность" form option prefix -> warmth, seasons, thickness, comfortable feels-like range
SEASON_OPTIONS = {
    "Лето":                 (1, ["лето"], "тонкая", [20, 40]),
    "Межсезонье — лёгкая":  (2, ["межсезонье"], "тонкая", [13, 22]),
    "Межсезонье — средняя": (3, ["межсезонье", "зима"], "средняя", [8, 18]),
    "Межсезонье — тёплая":  (4, ["межсезонье", "зима"], "плотная", [-2, 12]),
    "Зима":                 (5, ["зима"], "плотная", [-20, 5]),
}
SHOE_TEMP = {1: [18, 45], 2: [5, 35], 3: [0, 25], 4: [-10, 15], 5: [-25, 5]}
PREFIX = {"Верх": "top", "Низ": "bottom", "Верхняя одежда": "outer", "Обувь": "shoes", "Аксессуары": "acc"}
CONDITION = {"Отличное": "отличное", "Хорошее": "хорошее", "Есть дефекты": "есть дефекты", "Только для дома": "только дом/спорт"}
MONTHS = ["январ", "феврал", "март", "апрел", "ма[йяе]\\b", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр"]
SEASON_MONTH = {"зим": 1, "весн": 4, "лет": 7, "осен": 10}


# ---------------------------------------------------------------- parsing
def parse_form(body: str) -> dict:
    """GitHub issue forms render as '### Label\\n\\nvalue' blocks."""
    fields = {}
    for block in re.split(r"^### ", body, flags=re.M)[1:]:
        label, _, value = block.partition("\n")
        value = value.strip()
        fields[label.strip()] = "" if value == "_No response_" else value
    return fields


def get(f: dict, prefix: str) -> str:
    """Field value by label prefix (labels may carry hints in brackets)."""
    for k, v in f.items():
        if k.startswith(prefix):
            return v.strip()
    return ""


def checked(value: str) -> list[str]:
    return [m.strip() for m in re.findall(r"^- \[[xX]\] (.+)$", value or "", flags=re.M)]


def image_urls(text: str) -> list[str]:
    urls = re.findall(r"!\[[^\]]*\]\((https://[^)\s]+)\)", text)
    urls += re.findall(r'<img[^>]+src="(https://[^"]+)"', text)
    return list(dict.fromkeys(urls))


def parse_purchased(text: str, today: dt.date = TODAY) -> dict | None:
    """'осень 2024', 'март 2025', '2023', 'год назад', '2 года назад', 'полгода назад', 'недавно' -> {date: 'YYYY-MM', approx, text}."""
    t = text.strip().lower()
    if not t or t in {"не знаю", "не помню", "-"}:
        return None
    exact = re.fullmatch(r"(\d{1,2})[./](\d{4})", t) or re.fullmatch(r"(\d{4})-(\d{1,2})", t)
    if exact:
        a, b = exact.groups()
        y, m = (int(b), int(a)) if len(a) <= 2 else (int(a), int(b))
        return {"date": f"{y:04d}-{m:02d}", "approx": False, "text": text.strip()}

    def back(months: int):
        total = today.year * 12 + today.month - 1 - months
        return {"date": f"{total // 12:04d}-{total % 12 + 1:02d}", "approx": True, "text": text.strip()}

    if "недавно" in t:
        return back(0)
    if "полгода" in t:
        return back(6)
    m = re.search(r"(\d+(?:[.,]\d)?)?\s*(год|года|лет)\s+назад", t)
    if m:
        return back(round(float((m.group(1) or "1").replace(",", ".")) * 12))
    m = re.search(r"(\d+)?\s*месяц\w*\s+назад", t)
    if m:
        return back(int(m.group(1) or 1))
    year = re.search(r"(20\d{2}|19\d{2})", t)
    month = next((i + 1 for i, name in enumerate(MONTHS) if re.search(r"\b" + name, t)), None)
    season = next((mm for key, mm in SEASON_MONTH.items() if key in t), None)
    if year:
        mon = month or season or 6
        return {"date": f"{int(year.group(1)):04d}-{mon:02d}", "approx": not month, "text": text.strip()}
    if month or season:  # "в марте", "осенью" without a year -> most recent one in the past
        mon = month or season
        y = today.year if mon <= today.month else today.year - 1
        return {"date": f"{y:04d}-{mon:02d}", "approx": True, "text": text.strip()}
    return {"date": None, "approx": True, "text": text.strip()}


# ---------------------------------------------------------------- images
_session = None


def rembg_session():
    global _session
    if _session is None:
        from rembg import new_session
        _session = new_session("isnet-general-use")
    return _session


def cutout(img):
    from PIL import Image
    from rembg import remove
    out = remove(img, session=rembg_session(), post_process_mask=True)
    bbox = out.getchannel("A").point(lambda a: 255 if a > 20 else 0).getbbox()
    if bbox:
        out = out.crop(bbox)
    pad = int(max(out.size) * 0.06)
    canvas = Image.new("RGBA", (out.width + 2 * pad, out.height + 2 * pad), (0, 0, 0, 0))
    canvas.alpha_composite(out, (pad, pad))
    return canvas


def load(src):
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    img.thumbnail((2000, 2000))
    return img


def save(img, path: Path):
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    img.save(path, "WEBP", quality=82, method=6)




# ---------------------------------------------------------------- item updates
def log(item: dict, event: str, note: str = ""):
    item.setdefault("history", []).append({k: v for k, v in
                                           {"date": TODAY.isoformat(), "event": event, "note": note}.items() if v})


def next_id(items, prefix):
    nums = [int(m.group(1)) for i in items if (m := re.fullmatch(prefix + r"-(\d+)", i["id"]))]
    return f"{prefix}-{max(nums, default=0) + 1:03d}"


def apply_fields(item: dict, f: dict, is_new: bool) -> list[str]:
    """Copy filled form fields onto the item. Returns human-readable list of what changed."""
    changed = []

    def put(key, value, label):
        if value not in (None, "", []) and item.get(key) != value:
            item[key] = value
            changed.append(label)

    put("name", get(f, "Название"), "название")
    put("category", get(f, "Категория"), "категория")
    put("brand", get(f, "Бренд"), "бренд")
    put("material", get(f, "Состав"), "состав")
    put("size", get(f, "Размер"), "размер")
    put("link", get(f, "Ссылка"), "ссылка")
    put("price", get(f, "Цена"), "цена")
    put("notes", get(f, "Заметки"), "заметки")
    color = get(f, "Цвет")
    if color:
        item["color"] = colors.from_name(color, item.get("color"))
        changed.append("цвет")
    put("type", get(f, "Тип").lower(), "тип")
    put("pattern", get(f, "Узор").split(" —")[0].lower(), "узор")
    formality = get(f, "Стиль")
    if formality[:1].isdigit():
        put("formality", int(formality[0]), "стиль")
    put("insole_cm", get(f, "Длина стельки").replace(",", "."), "длина стельки")

    season = get(f, "Сезон и плотность")
    for key, (warmth, seasons, thickness, temp) in SEASON_OPTIONS.items():
        if season.startswith(key):
            if (get(f, "Категория") or item.get("category")) == "Обувь":
                temp = SHOE_TEMP[warmth]  # shoes are fine in a much wider range than clothes
            item.update(warmth=warmth, season=seasons, thickness=thickness, temp=temp)
            changed.append("сезон")
            break

    purchased = parse_purchased(get(f, "Когда купил"))
    if purchased:
        item["purchased"] = purchased
        changed.append("дата покупки")

    prot = get(f, "Защита от погоды")
    if prot:
        item["windproof"] = "[x] Держит ветер" in prot
        item["waterproof"] = "[x] Непромокаемая" in prot

    cond = get(f, "Состояние")
    for key, val in CONDITION.items():
        if cond.startswith(key) and item.get("condition") != val:
            item["condition"] = val
            changed.append("состояние")
    flaws = [x.lower() for x in checked(get(f, "Дефекты"))]
    if flaws:
        item["flaws"] = sorted(set(item.get("flaws", [])) | set(flaws))
        changed.append("дефекты")
    put("flaw_notes", get(f, "Подробнее о дефектах"), "описание дефекта")
    occ = checked(get(f, "Где можно носить"))
    if occ:
        item["occasions"] = occ
        changed.append("где носить")
    elif is_new and item.get("condition") == "только дом/спорт":
        item["occasions"] = ["Дом", "Спорт"]

    if is_new:
        cat = item.get("category", "Верх")
        item.setdefault("formality", 2)
        if cat == "Обувь":
            item.update(layer="обувь")
        item["layer"] = {"Верхняя одежда": "верхний", "Низ": "база", "Обувь": "обувь", "Аксессуары": "аксессуар"}.get(
            cat, "база" if item.get("warmth", 2) <= 1 else "средний")
    return changed


def new_item(items, f) -> dict:
    category = get(f, "Категория") or "Верх"
    item = {
        "id": next_id(items, PREFIX.get(category, "item")),
        "status": "active",
        "name": get(f, "Название") or "Новая вещь",
        "category": category,
        "color": {},
        "warmth": 2, "season": ["межсезонье"], "thickness": "тонкая", "temp": [13, 22],
        "windproof": False, "waterproof": False,
        "condition": "хорошее", "flaws": [], "occasions": [],
        "added": TODAY.isoformat(), "purchased": None,
        "photos": [],
    }
    items.append(item)
    return item


def add(data, f, token) -> str:
    import requests
    items = data["items"]
    urls = image_urls(get(f, "Фото"))
    existing = get(f, "Добавить фото к существующей вещи") or get(f, "ID существующей вещи")
    if existing:
        item = next((i for i in items if i["id"] == existing), None)
        if not item:
            raise SystemExit(f"Вещь с id «{existing}» не найдена.")
        is_new = False
    else:
        if not urls:
            raise SystemExit("Не нашёл ни одного фото. Прикрепи фото в поле «Фото».")
        item, is_new = new_item(items, f), True
    changed = apply_fields(item, f, is_new)

    mode = get(f, "Удалить фон") or "На всех фото"
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
        if start + n == 0:
            auto = colors.analyze(out)
            user = item.get("color") or {}
            if user.get("auto") is False and user.get("name"):
                item["color"] = {**auto, **user, "secondary": auto.get("secondary", [])}
            else:
                item["color"] = auto

    if is_new:
        log(item, "added")
    else:
        parts = ([f"фото +{len(urls)}"] if urls else []) + changed
        log(item, "updated", ", ".join(parts))
    lines = [f"Готово: «{item['name']}» (`{item['id']}`) — {'добавлена' if is_new else 'обновлена'}."]
    if urls:
        lines.append(f"Фото: +{len(urls)}.")
    if changed and not is_new:
        lines.append("Изменено: " + ", ".join(changed) + ".")
    if item.get("purchased"):
        lines.append(f"Куплена: {item['purchased']['text']}.")
    return "\n".join(lines)


def archive(data, f) -> str:
    item_id = get(f, "ID вещи")
    item = next((i for i in data["items"] if i["id"] == item_id), None)
    if not item:
        raise SystemExit(f"Вещь с id «{item_id}» не найдена.")
    action = get(f, "Что сделать")
    if action.startswith("Вернуть"):
        item["status"] = "active"
        item.pop("archived", None)
        log(item, "restored")
        return f"«{item['name']}» снова в гардеробе."
    reason = get(f, "Причина") or "другое"
    comment = get(f, "Комментарий")
    item["status"] = "archived"
    item["archived"] = {k: v for k, v in {"date": TODAY.isoformat(), "reason": reason, "comment": comment}.items() if v}
    log(item, "archived", reason)
    return f"«{item['name']}» убрана в архив ({reason.lower()}). Она больше не попадёт в образы, но останется в разделе «Архив»."


def main():
    f = parse_form(os.environ["ISSUE_BODY"])
    data = json.loads(DATA.read_text(encoding="utf-8"))
    form = os.environ.get("FORM", "add")
    msg = archive(data, f) if form == "archive" else add(data, f, os.environ.get("GITHUB_TOKEN", ""))
    try:  # never commit a catalogue that breaks the schema
        import jsonschema
        jsonschema.validate(data, json.loads((ROOT / "wardrobe" / "wardrobe.schema.json").read_text(encoding="utf-8")))
    except ImportError:
        pass
    DATA.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    msg += "\n\nСайт обновится через 1–2 минуты: https://ismoil77.github.io/project/#items"
    Path(os.environ.get("RESULT_FILE", "result.md")).write_text(msg, encoding="utf-8")
    print(msg)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "cutout":
        save(cutout(load(sys.argv[2])), Path(sys.argv[3]))
    elif len(sys.argv) == 3 and sys.argv[1] == "color":
        print(colors.analyze(load(sys.argv[2])))
    elif len(sys.argv) == 3 and sys.argv[1] == "purchased":
        print(parse_purchased(sys.argv[2]))
    else:
        main()
