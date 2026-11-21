"""Mirror manually chosen product photos into this repo (images/).

Rozetka hid items with "Фото не завантажуються, помилка доступу до ресурсу":
several shops/CDNs block hotlinking. Photos served from
raw.githubusercontent.com always load. build_feed.py swaps an override URL
for its mirrored copy when the file exists. Failed downloads are skipped
(the original URL stays) and never fail the build.
"""
import hashlib, json, os, sys
from urllib.request import Request, urlopen

ROOT = os.path.join(os.path.dirname(__file__), "..")
OVR = os.path.join(ROOT, "references", "picture_overrides.json")
OUT = os.path.join(ROOT, "images")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def mirror_name(url):
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def main():
    os.makedirs(OUT, exist_ok=True)
    have = {f.split(".")[0] for f in os.listdir(OUT)}
    urls = sorted(set(json.load(open(OVR, encoding="utf-8")).values()))
    new = fail = 0
    for u in urls:
        name = mirror_name(u)
        if name in have:
            continue
        try:
            r = urlopen(Request(u, headers={"User-Agent": UA, "Accept": "image/jpeg,image/png,image/*;q=0.8,*/*;q=0.5"}), timeout=30)
            ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
            data = r.read()
            if ctype not in EXT or len(data) < 2000:
                raise ValueError(f"not an image: {ctype} {len(data)}b")
            if ctype == "image/webp":  # Rozetka is safest with JPEG/PNG
                import io
                from PIL import Image
                im = Image.open(io.BytesIO(data))
                if im.mode in ("RGBA", "LA", "P"):
                    im = im.convert("RGBA")
                    bg = Image.new("RGB", im.size, (255, 255, 255))
                    bg.paste(im, mask=im.split()[-1])
                    im = bg
                else:
                    im = im.convert("RGB")
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=92)
                data, ctype = buf.getvalue(), "image/jpeg"
            open(os.path.join(OUT, name + EXT[ctype]), "wb").write(data)
            new += 1
        except Exception as e:  # noqa: BLE001 - best effort, keep going
            fail += 1
            print(f"  skip {u[:90]}: {e}", file=sys.stderr)
    print(f"mirror: {len(urls)} override photos, {new} downloaded now, {fail} failed")


if __name__ == "__main__":
    main()
