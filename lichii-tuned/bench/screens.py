"""Screens for Lichii Bench: a small PNG decoder and a difference hash of a thumbnail.

The debug server's `screenshot` returns a PNG (RGB, 8 bits, written by stb). Screens are
compared by a 256-bit difference hash of a 17x16 grey thumbnail: two shots of the same menu
differ by a few bits, two different screens by dozens. Standard library only.
"""

import struct
import zlib

HASH_W, HASH_H = 16, 16


class PngError(Exception):
    pass


def decode_png(data):
    """Returns (width, height, channels, rows) with rows as bytes, 8-bit non-interlaced only."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise PngError("not a PNG")
    pos = 8
    width = height = channels = None
    idat = []
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or interlace != 0:
                raise PngError(f"unsupported PNG: depth {depth}, interlace {interlace}")
            channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color)
            if channels is None:
                raise PngError(f"unsupported PNG colour type {color}")
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    if width is None:
        raise PngError("no IHDR")
    raw = zlib.decompress(b"".join(idat))
    stride = width * channels
    rows = []
    prev = bytearray(stride)
    pos = 0
    for _ in range(height):
        kind = raw[pos]
        line = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        if kind == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 255
        elif kind == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif kind == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif kind == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else b if pb <= pc else c
                line[i] = (line[i] + pred) & 255
        elif kind != 0:
            raise PngError(f"bad filter {kind}")
        rows.append(bytes(line))
        prev = line
    return width, height, channels, rows


def encode_png(width, height, rgb_rows):
    """A plain RGB PNG (filter 0); used by the tests and for saved thumbnails."""
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    raw = b"".join(b"\x00" + bytes(row) for row in rgb_rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def grey_grid(png, cols, rows_out):
    """Mean grey of each cell of a cols x rows_out grid over the image."""
    width, height, channels, rows = decode_png(png)
    grid = []
    for gy in range(rows_out):
        y0, y1 = gy * height // rows_out, max(gy * height // rows_out + 1, (gy + 1) * height // rows_out)
        line = []
        for gx in range(cols):
            x0, x1 = gx * width // cols, max(gx * width // cols + 1, (gx + 1) * width // cols)
            total = count = 0
            for y in range(y0, y1):
                row = rows[y]
                for x in range(x0, x1):
                    p = x * channels
                    if channels >= 3:
                        total += (row[p] * 299 + row[p + 1] * 587 + row[p + 2] * 114) // 1000
                    else:
                        total += row[p]
                    count += 1
            line.append(total / count)
        grid.append(line)
    return grid


def thumb_hash(png):
    """256-bit difference hash (hex) of a PNG."""
    grid = grey_grid(png, HASH_W + 1, HASH_H)
    bits = 0
    for line in grid:
        for x in range(HASH_W):
            bits = (bits << 1) | (1 if line[x] < line[x + 1] else 0)
    return format(bits, f"0{HASH_W * HASH_H // 4}x")


def mean_grey(png):
    grid = grey_grid(png, 8, 8)
    return sum(sum(line) for line in grid) / 64.0


def distance(a, b):
    """Bits that differ between two hashes; None if either is missing."""
    if a is None or b is None:
        return None
    return bin(int(a, 16) ^ int(b, 16)).count("1")
