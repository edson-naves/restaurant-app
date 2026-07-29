"""Menu-photo processing.

Downscales images so a full-resolution phone photo doesn't ship a multi-MB file
to a tablet for a thumbnail that renders under 50px. Pillow is imported lazily
and everything is wrapped so the app degrades gracefully: with Pillow absent (or
a file that isn't a decodable image) the bytes are written through unchanged.
That keeps local development and the test suites free of the dependency, while
the production image (which installs Pillow) gets the resizing.
"""
from __future__ import annotations

from pathlib import Path

# Longest edge, in pixels. The order screen and menu admin render these far
# smaller; 512 keeps them crisp on high-DPI screens with room to spare.
MAX_SIDE = 512


def save_image(source, dest: Path, max_side: int = MAX_SIDE) -> None:
    """Write an image to ``dest``, downscaled to fit ``max_side`` on its longest
    edge and re-encoded to shrink the file. ``source`` is a readable file object
    (e.g. an UploadFile's ``.file``) or a path. Never raises for a bad image —
    it falls back to storing the original bytes."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = source.read() if hasattr(source, "read") else Path(source).read_bytes()

    try:
        from io import BytesIO

        from PIL import Image

        im = Image.open(BytesIO(data))
        im.load()
        # Palette/grayscale-with-alpha don't save cleanly as-is; normalise.
        if im.mode in ("P", "LA"):
            im = im.convert("RGBA")

        im.thumbnail((max_side, max_side))  # only ever shrinks, keeps aspect

        suffix = dest.suffix.lower()
        if suffix in (".jpg", ".jpeg"):
            im.convert("RGB").save(dest, "JPEG", quality=82, optimize=True)
        elif suffix == ".webp":
            im.save(dest, "WEBP", quality=82, method=6)
        else:  # .png / .gif and anything else -> PNG
            im.save(dest, "PNG", optimize=True)
    except Exception:
        # Pillow missing, or not a decodable image — keep the original bytes.
        dest.write_bytes(data)


_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def resize_dir(directory: Path, max_side: int = MAX_SIDE) -> None:
    """Downscale every image in a folder in place. Used to shrink photos dropped
    straight into web/static/img/menu (the batch-import flow) so they match what
    the upload button produces."""
    before = after = 0
    for p in sorted(directory.glob("*")):
        if p.suffix.lower() not in _IMG_EXT:
            continue
        b = p.stat().st_size
        save_image(p, p, max_side)      # reads all bytes first -> safe in place
        a = p.stat().st_size
        before += b
        after += a
        print(f"  {p.name:30s} {b // 1024:6d} KB -> {a // 1024:5d} KB")
    if before:
        print(f"TOTAL: {before / 1048576:.1f} MB -> {after / 1048576:.2f} MB "
              f"({100 - after * 100 // before}% smaller)")
    else:
        print("No images found.")


if __name__ == "__main__":
    # `python -m app.services.images [dir]` — defaults to the menu photo folder.
    import sys

    default_dir = Path(__file__).resolve().parents[2] / "web" / "static" / "img" / "menu"
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else default_dir
    print(f"Resizing images in {target} (longest edge {MAX_SIDE}px)")
    resize_dir(target)
