from PIL import Image, ImageDraw
import os

BG_COLOR = (33, 31, 27)  # #211f1b — the card background from the preview
ACCENT = (196, 149, 106, 255)
ACCENT_DIM = (196, 149, 106, 140)

S = 512 / 140.0
YO = (512 - 120 * S) / 2.0

RAW_POINTS = [
    (12, 68), (32, 68), (42, 62), (50, 72),
    (66, 14), (84, 106), (96, 42), (104, 68),
    (112, 68), (128, 68),
]

ECG_POINTS = [(x * S, y * S + YO) for x, y in RAW_POINTS]
SPIKE_INDICES = [3, 4, 5, 6]
MAIN_W_BASE = 3.0 * S * 2.5
SPIKE_W_BASE = 4.5 * S * 2.5

def draw_round_line(draw, p1, p2, width, color):
    draw.line([p1, p2], fill=color, width=int(width))
    r = width / 2
    draw.ellipse([p1[0]-r, p1[1]-r, p1[0]+r, p1[1]+r], fill=color)
    draw.ellipse([p2[0]-r, p2[1]-r, p2[0]+r, p2[1]+r], fill=color)

def generate_logo(size, output_path):
    ss = 4
    ss_size = size * ss
    f = ss_size / 512.0

    points = [(x * f, y * f) for x, y in ECG_POINTS]
    main_w = max(MAIN_W_BASE * f, 2)
    spike_w = max(SPIKE_W_BASE * f, 3)

    img = Image.new('RGBA', (ss_size, ss_size), BG_COLOR + (255,))
    draw = ImageDraw.Draw(img)
    for i in range(len(points) - 1):
        draw_round_line(draw, points[i], points[i+1], main_w, ACCENT)

    overlay = Image.new('RGBA', (ss_size, ss_size), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for i in range(len(SPIKE_INDICES) - 1):
        p1 = points[SPIKE_INDICES[i]]
        p2 = points[SPIKE_INDICES[i+1]]
        draw_round_line(od, p1, p2, spike_w, ACCENT_DIM)

    img = Image.alpha_composite(img, overlay)
    img = img.resize((size, size), Image.LANCZOS)

    final = Image.new('RGB', (size, size), BG_COLOR)
    final.paste(img, mask=img.split()[3])
    final.save(output_path, 'PNG')
    print(f"  ok {output_path} ({size}x{size})")
    return final

output_dir = '/home/claude/icons'
os.makedirs(output_dir, exist_ok=True)

sizes = {
    16: 'favicon-16x16.png',
    32: 'favicon-32x32.png',
    180: 'apple-touch-icon.png',
    192: 'android-chrome-192x192.png',
    512: 'android-chrome-512x512.png',
}

images = {}
for size, name in sizes.items():
    images[size] = generate_logo(size, os.path.join(output_dir, name))

images[32].save(os.path.join(output_dir, 'favicon.ico'), format='ICO', sizes=[(16,16),(32,32)])
print("  ok favicon.ico")

svg_pts = " ".join(f"{'M' if i==0 else 'L'} {x:.1f},{y:.1f}" for i,(x,y) in enumerate(ECG_POINTS))
spike_pts = " ".join(f"{'M' if i==0 else 'L'} {ECG_POINTS[SPIKE_INDICES[i]][0]:.1f},{ECG_POINTS[SPIKE_INDICES[i]][1]:.1f}" for i in range(len(SPIKE_INDICES)))

with open(os.path.join(output_dir, 'loore-logo.svg'), 'w') as f:
    f.write(f'''<svg width="512" height="512" viewBox="0 0 512 512" fill="none" xmlns="http://www.w3.org/2000/svg">
  <rect width="512" height="512" fill="#211f1b"/>
  <path d="{svg_pts}" stroke="#c4956a" stroke-width="27" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="{spike_pts}" stroke="#c4956a" stroke-width="41" stroke-linecap="round" stroke-linejoin="round" opacity="0.55"/>
</svg>''')
print("  ok loore-logo.svg")

with open(os.path.join(output_dir, 'loore-logo-transparent.svg'), 'w') as f:
    f.write(f'''<svg width="512" height="512" viewBox="0 0 512 512" fill="none" xmlns="http://www.w3.org/2000/svg">
  <path d="{svg_pts}" stroke="#c4956a" stroke-width="27" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="{spike_pts}" stroke="#c4956a" stroke-width="41" stroke-linecap="round" stroke-linejoin="round" opacity="0.55"/>
</svg>''')
print("  ok loore-logo-transparent.svg")

print("\nDone!")
