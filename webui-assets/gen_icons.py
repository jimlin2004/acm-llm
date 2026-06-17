from PIL import Image

src = Image.open('/tmp/acm_logo.png').convert('RGBA')


def square(size, pad_ratio=0.08):
    canvas = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    max_w = int(size * (1 - 2 * pad_ratio))
    max_h = int(size * (1 - 2 * pad_ratio))
    r = min(max_w / src.width, max_h / src.height)
    w, h = int(src.width * r), int(src.height * r)
    im = src.resize((w, h), Image.LANCZOS)
    canvas.paste(im, ((size - w) // 2, (size - h) // 2), im)
    return canvas


B = '/app/build'
targets = {
    f'{B}/favicon.png': 256,
    f'{B}/static/favicon.png': 256,
    f'{B}/static/favicon-96x96.png': 96,
    f'{B}/static/favicon-dark.png': 256,
    f'{B}/static/apple-touch-icon.png': 180,
    f'{B}/static/splash.png': 512,
    f'{B}/static/splash-dark.png': 512,
    f'{B}/static/web-app-manifest-192x192.png': 192,
    f'{B}/static/web-app-manifest-512x512.png': 512,
}
for path, sz in targets.items():
    square(sz).save(path)
src.save(f'{B}/static/logo.png')
print("wrote", len(targets) + 1, "icon/logo files")
