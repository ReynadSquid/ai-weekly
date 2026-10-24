"""Статичный фон обложки: synthwave-закат, сетка и робот-репортёр (без текста).

    python branding/build_cover_base.py   → branding/cover_base.png (2560×1920)

Запускается вручную только при смене дизайна. Номер, даты и логотипы рисует scripts/make_cover.py.
Нужны Windows-шрифты (Impact для «AI» на кепке) — поэтому фон рендерится локально и коммитится.
"""
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_avatar as av  # noqa: E402  — тот же робот и палитра, что у аватарки

W, H = 2560, 1920
HORIZON = int(H * 0.74)
OUT = Path(__file__).resolve().parent


def background():
    img = av.vgradient((W, H), [(0, (14, 6, 40)), (0.5, (74, 20, 120)), (0.72, (190, 40, 130)),
                                (1, (255, 94, 126))]).convert("RGBA")
    d = ImageDraw.Draw(img)
    rnd = random.Random(11)
    for _ in range(220):
        x, y, r = rnd.randrange(W), rnd.randrange(0, int(H * 0.6)), rnd.choice([3, 4, 5, 7])
        d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, rnd.randrange(110, 255)))
    cx, cy, R = int(W * 0.76), int(H * 0.5), 620
    sun = av.vgradient((2 * R, 2 * R), [(0, (255, 226, 92)), (0.55, (255, 130, 90)), (1, av.PINK)]).convert("RGBA")
    mask = Image.new("L", (2 * R, 2 * R), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse([0, 0, 2 * R - 1, 2 * R - 1], fill=255)
    y, gap = int(R * 1.05), 14
    while y < 2 * R:
        md.rectangle([0, y, 2 * R, y + gap], fill=0)
        y += gap + 50
        gap += 8
    sun.putalpha(mask)
    halo = Image.new("RGBA", (W, H))
    halo.alpha_composite(sun, (cx - R, cy - R))
    av.glow(img, halo, 90, 1)
    floor = Image.new("RGBA", (W, H))
    ImageDraw.Draw(floor).rectangle([0, HORIZON, W, H], fill=(18, 6, 43, 255))
    lines = Image.new("RGBA", (W, H))
    ld = ImageDraw.Draw(lines)
    vx = int(W * 0.62)
    for i in range(-20, 21):
        ld.line([(vx, HORIZON), (vx + i * 300, H + 500)], fill=av.CYAN + (255,), width=6)
    k = 0
    while (y := HORIZON + int(16 * (1.4 ** k))) <= H:
        ld.line([(0, y), (W, y)], fill=av.CYAN + (255,), width=6)
        k += 1
    ld.line([(0, HORIZON), (W, HORIZON)], fill=(255, 140, 220, 255), width=10)
    img.alpha_composite(floor)
    av.glow(img, lines, 14, 2)
    return img


def main():
    img = background()
    bot = av.robot()
    bot.alpha_composite(av.cap())
    bot = bot.crop(bot.getbbox())
    h = 1080
    bot = bot.resize((int(bot.width * h / bot.height), h), Image.LANCZOS)
    img.alpha_composite(bot, (int(W * 0.8) - bot.width // 2, HORIZON + 140 - bot.height))
    img.convert("RGB").save(OUT / "cover_base.png", optimize=True)
    print("ok")


if __name__ == "__main__":
    main()
