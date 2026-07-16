"""Generate a clean, brand-new USB2Net application icon (USB2Net.ico).

Design: a rounded-square gradient tile with a white "bridge" motif -
a serial/USB device on the left connecting through a channel to a
network node on the right, conveying "serial <-> network bridge".
"""

import math
from PIL import Image, ImageDraw


def lerp(a, b, t):
    return int(a + (b - a) * t)


def vgradient(size, top, bottom):
    """Vertical gradient background as RGBA image."""
    w, h = size
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    px = img.load()
    for y in range(h):
        t = y / (h - 1)
        r = lerp(top[0], bottom[0], t)
        g = lerp(top[1], bottom[1], t)
        b = lerp(top[2], bottom[2], t)
        for x in range(w):
            px[x, y] = (r, g, b, 255)
    return img


def round_rect(draw, box, radius, fill=None, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def main():
    S = 256
    img = vgradient((S, S), (30, 58, 138), (14, 165, 233))  # #1E3A8A -> #0EA5E9
    d = ImageDraw.Draw(img)

    # subtle inner highlight border
    round_rect(
        d, (10, 10, S - 11, S - 11), radius=48, outline=(255, 255, 255, 40), width=3
    )

    WHITE = (255, 255, 255, 235)
    cy = S // 2

    # --- left: serial/USB device (rounded square with two contact ticks) ---
    dev = (40, cy - 46, 104, cy + 46)
    round_rect(d, dev, radius=18, fill=WHITE)
    # two connector pins on the right edge of the device
    d.rectangle((104, cy - 22, 120, cy - 10), fill=(30, 58, 138, 255))
    d.rectangle((104, cy + 10, 120, cy + 22), fill=(30, 58, 138, 255))

    # --- right: network node (circle with orbit) ---
    node_c = (184, cy)
    r = 40
    d.ellipse((node_c[0] - r, node_c[1] - r, node_c[0] + r, node_c[1] + r), fill=WHITE)
    # simple globe arcs on the node
    d.ellipse(
        (node_c[0] - r, node_c[1] - r, node_c[0] + r, node_c[1] + r),
        outline=(14, 165, 233, 255),
        width=4,
    )
    d.line(
        (node_c[0] - r, node_c[1], node_c[0] + r, node_c[1]),
        fill=(14, 165, 233, 255),
        width=4,
    )
    d.line(
        (node_c[0], node_c[1] - r, node_c[0], node_c[1] + r),
        fill=(14, 165, 233, 255),
        width=4,
    )
    # vertical ellipse arc (longitude)
    d.arc(
        (node_c[0] - r, node_c[1] - r, node_c[0] + r, node_c[1] + r),
        start=60,
        end=120,
        fill=(14, 165, 233, 255),
        width=4,
    )

    # --- connection channel between device and node ---
    d.line((120, cy, 144, cy), fill=WHITE, width=10)
    # pulse dots along the bridge
    for i, fx in enumerate((150, 162, 174)):
        rr = 5 - i
        if rr > 0:
            d.ellipse((fx - rr, cy - rr, fx + rr, cy + rr), fill=WHITE)

    out = "src/USB2Net.ico"
    img.save(
        out, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    )
    print("wrote", out)

    # 同时导出一张 PNG，供运行时 tkinter iconphoto 使用（兼容性最好）
    png = "src/USB2Net.png"
    img.save(png)
    print("wrote", png)


if __name__ == "__main__":
    main()
