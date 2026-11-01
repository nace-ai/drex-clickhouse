"""Solid-colour PNG test images, generated in code so no image file or customer content is committed.

The images are 320 x 320, inside prompt_jev's 280 to 1,568 pixels per side and well under Drex's limits, so the live
job can send the same bytes to a real image model.

Run: python3 test/mock/pngs.py <directory> writes <colour>.png there for each colour.
"""

import struct
import sys
import zlib
from pathlib import Path

SIZE = 320

COLOURS = {
    "red": (220, 30, 30),
    "blue": (30, 60, 220),
}


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def solid_png(rgb: tuple[int, int, int], width: int = SIZE, height: int = SIZE) -> bytes:
    """An 8-bit RGB PNG of one colour."""
    row = b"\x00" + bytes(rgb) * width  # filter type 0 (None) on every scanline
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(row * height, 9))
        + _chunk(b"IEND", b"")
    )


def fixture(name: str) -> bytes:
    return solid_png(COLOURS[name])


def first_pixel(png: bytes) -> tuple[int, int, int] | None:
    """The RGB of the top-left pixel of an 8-bit RGB or RGBA PNG, or None for anything else.

    Row 0's first pixel is stored unchanged under every PNG filter type, so no unfiltering is needed.
    """
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    pos, idat, channels = 8, b"", None
    while pos + 8 <= len(png):
        (length,) = struct.unpack(">I", png[pos : pos + 4])
        kind, data = png[pos + 4 : pos + 8], png[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            depth, colour_type = data[8], data[9]
            if depth != 8 or colour_type not in (2, 6):
                return None
            channels = 3 if colour_type == 2 else 4
        elif kind == b"IDAT":
            idat += data
        elif kind == b"IEND":
            break
    if channels is None or not idat:
        return None
    try:
        raw = zlib.decompressobj().decompress(idat, 1 + channels)
    except zlib.error:
        return None
    if len(raw) < 4:
        return None
    return raw[1], raw[2], raw[3]


def colour_of(png: bytes) -> str:
    """'red', 'blue' or 'unknown', by the dominant channel of the top-left pixel."""
    pixel = first_pixel(png)
    if pixel is None:
        return "unknown"
    r, g, b = pixel
    if r > g and r > b:
        return "red"
    if b > r and b > g:
        return "blue"
    return "unknown"


if __name__ == "__main__":
    directory = Path(sys.argv[1])
    directory.mkdir(parents=True, exist_ok=True)
    for name in COLOURS:
        (directory / f"{name}.png").write_bytes(fixture(name))
