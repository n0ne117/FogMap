"""Compose the rendered demo tiles into pictures for the website.

The fog and the trails are genuine output of Irfaran's renderer. The data
under them is invented - a seeded random walk on a grid in the middle of the
Atlantic - and the streets showing through the cleared fog are a drawn
stand-in for the basemap, which is third-party map data and not ours to put
in a screenshot. Nobody's real movements appear anywhere in these images.
"""
import random
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

TILES = Path('/out/tiles/dark')
GROUND = (40, 44, 52)
BLOCK = (55, 61, 72)
STREET = (128, 138, 156)
PARK = (44, 62, 50)


def backdrop(w: int, h: int) -> Image.Image:
    """A stand-in for the map underneath, so cleared fog has something to clear."""
    random.seed(11)
    img = Image.new('RGBA', (w, h), GROUND + (255,))
    draw = ImageDraw.Draw(img)
    for x in range(-40, w + 40, 64):
        for y in range(-40, h + 40, 52):
            roll = random.random()
            if roll < 0.12:
                draw.rectangle([x + 6, y + 5, x + 52, y + 40], fill=PARK + (255,))
            elif roll < 0.84:
                draw.rectangle([x + 6, y + 5, x + 52, y + 40], fill=BLOCK + (255,))
    for x in range(-40, w + 40, 64):
        draw.line([(x, 0), (x, h)], fill=STREET + (255,), width=4)
    for y in range(-40, h + 40, 52):
        draw.line([(0, y), (w, y)], fill=STREET + (255,), width=4)
    return img.filter(ImageFilter.GaussianBlur(0.4))


def sheet(view: str, zoom: int, out: str, crop=None, fog_alpha=0.86) -> None:
    have = sorted((TILES / view / 'fog' / str(zoom)).glob('*/*.png'))
    if not have:
        print('nothing at', view, zoom)
        return
    xs = sorted({int(p.parent.name) for p in have})
    ys = sorted({int(p.stem) for p in have})
    w, h = len(xs) * 256, len(ys) * 256

    canvas = backdrop(w, h)
    # Solid fog first, everywhere. A tile with no data is not a hole in the
    # map, it is ground nobody has been - which is exactly what the app serves
    # for a tile that was never rendered.
    solid = Image.new('RGBA', (w, h), (28, 30, 35, 255))

    for kind, target in (('trail', None), ('fog', solid)):
        layer = Image.new('RGBA', (w, h), (0, 0, 0, 0))
        for i, x in enumerate(xs):
            for j, y in enumerate(ys):
                tile = TILES / view / kind / str(zoom) / str(x) / f'{y}.png'
                if tile.exists():
                    layer.paste(Image.open(tile), (i * 256, j * 256))
                elif target is not None:
                    layer.paste(target.crop((0, 0, 256, 256)), (i * 256, j * 256))
        if kind == 'fog':
            layer.putalpha(layer.getchannel('A').point(lambda a: int(a * fog_alpha)))
        canvas = Image.alpha_composite(canvas, layer)

    if crop:
        canvas = canvas.crop(crop)
    canvas.convert('RGB').save(f'/out/{out}', quality=92)
    print(out, canvas.size)


sheet('all', 15, 'hero.jpg', crop=(120, 380, 1160, 900))
sheet('all', 16, 'detail.jpg', crop=(500, 900, 1780, 1620))
sheet('year-2024', 15, 'year.jpg', crop=(120, 380, 1160, 900))
