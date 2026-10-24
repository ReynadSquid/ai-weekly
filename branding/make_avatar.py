"""Аватарка бота Lil_News: робот-репортёр в кепке на фоне synthwave-заката.

    python branding/make_avatar.py   → branding/lil_news_avatar.png (1024×1024) и превью в круге
"""
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

S = 2048  # рисуем крупно, потом уменьшаем — сглаживание
OUT = Path(__file__).resolve().parent
INK = (20, 10, 46)
CYAN = (62, 240, 255)
PINK = (255, 61, 127)
CREAM = (246, 242, 255)
MUSTARD = (255, 182, 39)
HORIZON = 1430


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def vgradient(size, stops):
    w, h = size
    img = Image.new("RGB", size)
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
            if t0 <= t <= t1:
                d.line([(0, y), (w, y)], fill=lerp(c0, c1, (t - t0) / (t1 - t0)))
                break
    return img


def glow(base, layer, radius, strength=2):
    blurred = layer.filter(ImageFilter.GaussianBlur(radius))
    for _ in range(strength):
        base.alpha_composite(blurred)
    base.alpha_composite(layer)


def background():
    img = vgradient((S, S), [(0, (14, 6, 40)), (0.45, (74, 20, 120)), (0.7, (190, 40, 130)), (1, (255, 94, 126))])
    img = img.convert("RGBA")
    d = ImageDraw.Draw(img)
    rnd = random.Random(7)
    for _ in range(140):  # звёзды
        x, y, r = rnd.randrange(S), rnd.randrange(0, 900), rnd.choice([3, 4, 5, 7])
        d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, rnd.randrange(120, 255)))

    # солнце с полосами
    cx, cy, R = S // 2, 1090, 640
    sun = vgradient((2 * R, 2 * R), [(0, (255, 226, 92)), (0.55, (255, 130, 90)), (1, PINK)]).convert("RGBA")
    mask = Image.new("L", (2 * R, 2 * R), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse([0, 0, 2 * R - 1, 2 * R - 1], fill=255)
    y, gap = int(R * 1.05), 14
    while y < 2 * R:
        md.rectangle([0, y, 2 * R, y + gap], fill=0)
        y += gap + 50
        gap += 8
    sun.putalpha(mask)
    halo = Image.new("RGBA", (S, S))
    halo.alpha_composite(sun, (cx - R, cy - R))
    glow(img, halo, 90, 1)

    # пол-сетка ниже горизонта
    floor = Image.new("RGBA", (S, S))
    fd = ImageDraw.Draw(floor)
    fd.rectangle([0, HORIZON, S, S], fill=(18, 6, 43, 255))
    lines = Image.new("RGBA", (S, S))
    ld = ImageDraw.Draw(lines)
    for i in range(-14, 15):
        ld.line([(S // 2, HORIZON), (S // 2 + i * 260, S + 400)], fill=CYAN + (255,), width=6)
    k = 0
    while True:
        y = HORIZON + int(18 * (1.35 ** k))
        if y > S:
            break
        ld.line([(0, y), (S, y)], fill=CYAN + (255,), width=6)
        k += 1
    ld.line([(0, HORIZON), (S, HORIZON)], fill=(255, 140, 220, 255), width=10)
    img.alpha_composite(floor)
    glow(img, lines, 14, 2)
    return img


def robot():
    lay = Image.new("RGBA", (S, S))
    d = ImageDraw.Draw(lay)
    ow = 34
    # уши-динамики
    for x0 in (452, 1476):
        d.rounded_rectangle([x0, 1010, x0 + 120, 1290], 50, fill=PINK, outline=INK, width=ow)
    # голова
    d.rounded_rectangle([540, 780, 1508, 1590], 230, fill=CREAM, outline=INK, width=ow)
    # блик на корпусе
    d.rounded_rectangle([610, 840, 760, 880], 20, fill=(255, 255, 255))
    # экран-лицо
    d.rounded_rectangle([650, 930, 1398, 1430], 150, fill=INK)
    # щёчки
    for cx in (600, 1448):
        d.ellipse([cx - 40, 1450, cx + 40, 1510], fill=(255, 150, 190))

    face = Image.new("RGBA", (S, S))
    fd = ImageDraw.Draw(face)
    # глаз-пилюля и подмигивание «^»
    fd.rounded_rectangle([800, 1040, 910, 1230], 55, fill=CYAN)
    fd.line([(1120, 1170), (1185, 1080), (1250, 1170)], fill=CYAN, width=44, joint="curve")
    for x, y in ((1120, 1170), (1250, 1170), (1185, 1080)):
        fd.ellipse([x - 22, y - 22, x + 22, y + 22], fill=CYAN)
    # улыбка
    fd.arc([900, 1180, 1150, 1360], 20, 160, fill=CYAN, width=40)
    glow(lay, face, 26, 2)
    ImageDraw.Draw(lay).ellipse([830, 1065, 865, 1100], fill=(255, 255, 255))  # блик в глазу
    return lay


def cap():
    lay = Image.new("RGBA", (S, S))
    d = ImageDraw.Draw(lay)
    ow = 34
    # антенна
    d.line([(1040, 470), (1110, 250)], fill=INK, width=28)
    spark = Image.new("RGBA", (S, S))
    sd = ImageDraw.Draw(spark)
    sd.ellipse([1060, 170, 1170, 280], fill=CYAN, outline=INK, width=20)
    # купол кепки-«газетчицы»; швы обрезаны по куполу
    dome_box = [500, 400, 1560, 1160]
    d.pieslice(dome_box, 180, 360, fill=MUSTARD, outline=INK, width=ow)
    seams = Image.new("RGBA", (S, S))
    sd2 = ImageDraw.Draw(seams)
    for x in (760, 1300):
        sd2.arc([x - 500, 430, x + 500, 1500], 240, 300, fill=(214, 135, 10), width=14)
    sd2.line([(1030, 440), (1030, 760)], fill=(214, 135, 10), width=14)
    dome_mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(dome_mask).pieslice([b + (40 if i < 2 else -40) for i, b in enumerate(dome_box)], 180, 360, fill=255)
    lay.paste(seams, (0, 0), Image.composite(seams.getchannel("A"), Image.new("L", (S, S), 0), dome_mask))
    # козырёк
    d.chord([560, 690, 1700, 960], 0, 180, fill=(232, 150, 20), outline=INK, width=ow)
    # пуговка
    d.ellipse([985, 380, 1085, 470], fill=PINK, outline=INK, width=24)
    # лента и пресс-карта «AI»
    d.rectangle([520, 700, 1540, 790], fill=INK)
    d.polygon([(1180, 560), (1420, 610), (1385, 800), (1145, 750)], fill=CREAM, outline=INK)
    d.line([(1180, 560), (1420, 610), (1385, 800), (1145, 750), (1180, 560)], fill=INK, width=18)
    font = ImageFont.truetype("C:/Windows/Fonts/impact.ttf", 150)
    txt = Image.new("RGBA", (300, 200))
    ImageDraw.Draw(txt).text((150, 100), "AI", font=font, fill=PINK, anchor="mm")
    txt = txt.rotate(-11, resample=Image.BICUBIC, expand=True)
    lay.alpha_composite(txt, (1282 - txt.width // 2, 680 - txt.height // 2))
    glow(lay, spark, 30, 3)
    lay = lay.rotate(5, resample=Image.BICUBIC, center=(1024, 780))
    shifted = Image.new("RGBA", (S, S))
    shifted.alpha_composite(lay, (0, 45))  # кепка сидит плотно на голове, без щели
    return shifted


def main():
    img = background()
    img.alpha_composite(robot())
    img.alpha_composite(cap())
    final = img.convert("RGB").resize((1024, 1024), Image.LANCZOS)
    final.save(OUT / "lil_news_avatar.png", optimize=True)

    # превью: как увидит Telegram (круг) в двух размерах
    prev = Image.new("RGB", (1240, 700), (24, 33, 44))
    for size, x in ((560, 40), (160, 700), (64, 960)):
        icon = final.resize((size, size), Image.LANCZOS)
        m = Image.new("L", (size, size), 0)
        ImageDraw.Draw(m).ellipse([0, 0, size - 1, size - 1], fill=255)
        prev.paste(icon, (x, 70), m)
    prev.save(OUT / "preview_circle.png")
    print("ok")


if __name__ == "__main__":
    main()
