"""Build the LingJingAPI vector logo and Windows icon (requires Pillow)."""

from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "app" / "gui" / "assets"
SIZE = 512
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
BRACKETS = (((178, 164), (92, 256), (178, 348)),
            ((334, 164), (420, 256), (334, 348)))
NODES = ((220, 256), (292, 256))


def build_assets() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"
    role="img" aria-labelledby="title">
  <title id="title">LingJingAPI — connected compute gateway</title>
  <defs>
    <linearGradient id="background" x1="0" y1="1" x2="1" y2="0">
      <stop stop-color="#1242ef"/>
      <stop offset="1" stop-color="#8735ff"/>
    </linearGradient>
  </defs>
  <rect width="512" height="512" rx="112" fill="url(#background)"/>
  <g fill="none" stroke="#fff" stroke-width="30" stroke-linecap="round"
     stroke-linejoin="round">
    <path d="M178 164 L92 256 L178 348"/>
    <path d="M334 164 L420 256 L334 348"/>
    <path d="M220 256 H292" stroke-width="18"/>
  </g>
  <g fill="#fff">
    <circle cx="220" cy="256" r="22"/>
    <circle cx="292" cy="256" r="22"/>
  </g>
</svg>
'''
    (ASSETS / "logo.svg").write_text(svg, encoding="utf-8")

    # Render the same vector geometry with supersampling for small tray icons.
    scale = 4
    canvas_size = SIZE * scale
    image = Image.new("RGBA", (canvas_size, canvas_size))
    pixels = image.load()
    start, end = (18, 66, 239), (135, 53, 255)
    for y in range(canvas_size):
        for x in range(canvas_size):
            blend = (x + canvas_size - 1 - y) / (2 * (canvas_size - 1))
            pixels[x, y] = tuple(round(a + (b - a) * blend)
                                 for a, b in zip(start, end)) + (255,)
    alpha = Image.new("L", image.size, 0)
    ImageDraw.Draw(alpha).rounded_rectangle(
        (0, 0, canvas_size - 1, canvas_size - 1), radius=112 * scale, fill=255
    )
    image.putalpha(alpha)
    draw = ImageDraw.Draw(image)

    def stroke(points, width):
        coords = [(x * scale, y * scale) for x, y in points]
        draw.line(coords, fill="white", width=width * scale, joint="curve")
        radius = width * scale / 2
        for x, y in coords:
            draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                         fill="white")

    for bracket in BRACKETS:
        stroke(bracket, 30)
    stroke(NODES, 18)
    for x, y in NODES:
        draw.ellipse(((x - 22) * scale, (y - 22) * scale,
                      (x + 22) * scale, (y + 22) * scale), fill="white")
    image = image.resize((SIZE, SIZE), Image.Resampling.LANCZOS)
    image.save(ROOT / "icon.png")
    image.save(ASSETS / "app.ico", sizes=[(s, s) for s in ICON_SIZES])
    print("Updated LingJingAPI logo.svg, icon.png and app.ico")


if __name__ == "__main__":
    build_assets()
