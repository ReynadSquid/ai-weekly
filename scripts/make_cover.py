"""Обложка выпуска: фирменный фон + «AI WEEKLY», «#N · даты» и логотипы компаний выпуска.

Использование:
    python scripts/make_cover.py --run 2026-10-25_weekly   → runs/<id>/cover.jpg

Данные — только из проверенного: init.json (номер, даты) и editor_input.json (компании новостей
со статусом verified/partially; слухи на обложку не попадают). Текста новостей на обложке нет.
Логотипы — branding/logos/<slug>.svg; соответствие компании и логотипа — поле `logo` в
config/watchlist.yaml или `logo_aliases`. Компании без логотипа сворачиваются в плитку «+N».
Код выхода 0 — обложка создана, 1 — не удалось (выпуск тогда уходит без обложки).
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from pathlib import Path

from common import ROOT, load_yaml, read_json, run_dir, setup_stdio

BRAND = ROOT / "branding"
FONT_TITLE = BRAND / "fonts" / "RussoOne-Regular.ttf"
FONT_TEXT = BRAND / "fonts" / "Exo2-Variable.ttf"
INK, CYAN, PINK, CREAM, MUSTARD = (20, 10, 46), (62, 240, 255), (255, 61, 127), (246, 242, 255), (255, 182, 39)
TILE, TILE_GAP, PER_ROW, MAX_TILES = 300, 46, 4, 12  # 3 ряда по 4
X0 = 140


def ensure_deps(install=True) -> str:
    """Pillow и resvg-py, пригодные для текущего Python.

    В облачном образе есть системный Pillow (/usr/lib/python3/dist-packages), собранный под другую
    версию Python: `pip install pillow` считает его установленным, а импорт падает на PIL._imaging.
    Тогда ставим свои копии поверх (--ignore-installed) и импортируем заново."""
    import importlib

    def _try():
        from PIL import Image  # noqa: F401  — падает на сломанном системном Pillow
        import resvg_py  # noqa: F401
        import PIL
        return PIL.__version__

    try:
        return _try()
    except ImportError as first:
        if not install:
            raise
        import os
        import subprocess

        env = dict(os.environ, PIP_BREAK_SYSTEM_PACKAGES="1")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--ignore-installed", "pillow", "resvg-py"],
                       check=False, env=env, capture_output=True, timeout=600)
        for name in [m for m in sys.modules if m.split(".")[0] in ("PIL", "resvg_py")]:
            del sys.modules[name]
        importlib.invalidate_caches()
        try:
            return _try()
        except ImportError as second:
            raise ImportError(f"{second} (до переустановки: {first})") from second


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def logo_map() -> dict[str, str]:
    """Нормализованное имя компании → slug логотипа."""
    wl = load_yaml("watchlist.yaml") or {}
    m = {}
    for c in wl.get("companies", []):
        if c.get("logo"):
            m[_norm(c["name"])] = c["logo"]
    for name, slug in (wl.get("logo_aliases") or {}).items():
        m[_norm(name)] = slug
    return m


def resolve_logo(company: str, mapping: dict[str, str]) -> str | None:
    slug = mapping.get(_norm(company)) or _norm(company)
    return slug if (BRAND / "logos" / f"{slug}.svg").exists() else None


def company_counts(editor_input: dict) -> list[tuple[str, int]]:
    """Компании подтверждённых новостей по убыванию числа новостей (порядок внутри — как в выпуске)."""
    from companies import split_companies

    counts: dict[str, int] = {}
    for it in editor_input.get("items", []):
        if it.get("kind") != "news":
            continue
        for c in split_companies(it.get("company")):  # «NVIDIA / Google Cloud» → обе компании
            counts[c] = counts.get(c, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])


def plan_tiles(companies: list[tuple[str, int]], mapping: dict[str, str]) -> list[dict]:
    """Какие плитки рисовать: до 12 логотипов, остальное — «+N». Чистая функция, покрыта тестами."""
    with_logo = [(c, n, s) for c, n in companies if (s := resolve_logo(c, mapping))]
    if len(companies) > len(with_logo) or len(with_logo) > MAX_TILES:
        with_logo = with_logo[:MAX_TILES - 1]
    tiles = [{"company": c, "count": n, "logo": s} for c, n, s in with_logo]
    rest = len(companies) - len(tiles)
    if rest:
        tiles.append({"company": f"+{rest}", "count": 0, "logo": None})
    return tiles


def _label(company: str) -> str:
    wl = load_yaml("watchlist.yaml") or {}
    return (wl.get("logo_labels") or {}).get(company, company)


def render_logo(slug: str, size: int):
    import resvg_py
    from PIL import Image

    svg = (BRAND / "logos" / f"{slug}.svg").read_text(encoding="utf-8")
    svg = (svg.replace("currentColor", "#%02x%02x%02x" % CREAM)
              .replace('width="1em"', 'width="24"').replace('height="1em"', 'height="24"'))
    png = resvg_py.svg_to_bytes(svg_string=svg, width=size, height=size, skip_system_fonts=True)
    return Image.open(io.BytesIO(bytes(png))).convert("RGBA")


def _glow(base, layer, radius, strength=1):
    from PIL import ImageFilter

    blurred = layer.filter(ImageFilter.GaussianBlur(radius))
    for _ in range(strength):
        base.alpha_composite(blurred)
    base.alpha_composite(layer)


def build_cover(title: str, issue_label: str, tiles: list[dict], out: Path) -> Path:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.open(BRAND / "cover_base.png").convert("RGBA")
    W, H = img.size

    # заголовок с неоновым свечением
    tf = ImageFont.truetype(str(FONT_TITLE), 250)
    text = title.upper()
    while tf.getlength(text) > W * 0.6 and tf.size > 120:
        tf = ImageFont.truetype(str(FONT_TITLE), tf.size - 10)
    glow_l = Image.new("RGBA", (W, H))
    ImageDraw.Draw(glow_l).text((X0, 120), text, font=tf, fill=CYAN + (255,))
    _glow(img, glow_l, 30, 2)
    d = ImageDraw.Draw(img)
    d.text((X0 + 8, 128), text, font=tf, fill=INK)
    d.text((X0, 120), text, font=tf, fill=CREAM)

    # плашка «#N · даты»
    pf = ImageFont.truetype(str(FONT_TEXT), 92)
    pf.set_variation_by_name("Bold")
    tw = d.textlength(issue_label, font=pf)
    d.rounded_rectangle([X0, 470, X0 + tw + 100, 610], 70, fill=PINK, outline=INK, width=10)
    d.text((X0 + 50, 540), issue_label, font=pf, fill=CREAM, anchor="lm")

    # плитки логотипов
    lf = ImageFont.truetype(str(FONT_TEXT), 46)
    lf.set_variation_by_name("SemiBold")
    bf = ImageFont.truetype(str(FONT_TITLE), 60)
    pluf = ImageFont.truetype(str(FONT_TITLE), 120)
    lay = Image.new("RGBA", (W, H))
    ld = ImageDraw.Draw(lay)
    y0 = 700
    for i, t in enumerate(tiles):
        x = X0 + (i % PER_ROW) * (TILE + TILE_GAP)
        y = y0 + (i // PER_ROW) * (TILE + TILE_GAP)
        hot = t["count"] >= 2
        ld.rounded_rectangle([x, y, x + TILE, y + TILE], 56, fill=INK + (225,),
                             outline=(MUSTARD if hot else CYAN) + (255,), width=10 if hot else 6)
        if not t["logo"]:
            ld.text((x + TILE // 2, y + TILE // 2), t["company"], font=pluf, fill=CREAM, anchor="mm")
            continue
        lay.alpha_composite(render_logo(t["logo"], 150), (x + (TILE - 150) // 2, y + 42))
        label = _label(t["company"])
        f = lf
        while f.getlength(label) > TILE - 30 and f.size > 28:
            f = ImageFont.truetype(str(FONT_TEXT), f.size - 4)
            f.set_variation_by_name("SemiBold")
        ld.text((x + TILE // 2, y + TILE - 52), label, font=f, fill=CREAM, anchor="mm")
        if hot:
            ld.ellipse([x + TILE - 78, y - 24, x + TILE + 24, y + 78], fill=PINK, outline=INK, width=8)
            ld.text((x + TILE - 27, y + 27), str(t["count"]), font=bf, fill=CREAM, anchor="mm")
    _glow(img, lay, 18, 1)

    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").resize((W // 2, H // 2), Image.LANCZOS).save(out, "JPEG", quality=90, optimize=True)
    return out


def make_for_run(run_id: str) -> Path:
    ensure_deps()
    rd = run_dir(run_id)
    init = read_json(rd / "init.json")
    ed = read_json(rd / "editor_input.json")
    if not init or not ed:
        raise FileNotFoundError("нет init.json или editor_input.json")
    tiles = plan_tiles(company_counts(ed), logo_map())
    if not tiles:
        raise ValueError("в выпуске нет компаний для обложки")
    label = f"#{init.get('issue_number', 1)} · {init.get('date_range', '')}".strip(" ·")
    return build_cover(init.get("title", "AI Weekly"), label, tiles, rd / "cover.jpg")


def main(argv=None) -> int:
    setup_stdio()
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--run")
    g.add_argument("--check", action="store_true", help="проверить/починить Pillow и resvg-py")
    args = ap.parse_args(argv)
    if args.check:
        try:
            print(json.dumps({"ok": True, "pillow": ensure_deps(), "python": sys.version.split()[0]}))
            return 0
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
            return 1
    try:
        path = make_for_run(args.run)
    except Exception as e:  # обложка необязательна — выпуск уйдёт без неё
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "path": str(path.relative_to(ROOT))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
