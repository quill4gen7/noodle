"""Read a Grasshopper `.gh` file without Rhino.

A `.gh` is a GH_IO archive compressed with raw DEFLATE (zlib wbits=-15). Inside
is a tree of CHUNKS, each one written as

    name      .NET string (7-bit encoded length + UTF-8)
    index     int32 (-1 = none; `Object#3` has index 3)
    n_items   int32
    n_chunks  int32
    items     n_items x (name, index, type code int32, value)
    chunks    n_chunks x chunk          (recursively)

There are no chunk lengths, so every value type must be known to be skipped;
the type codes are the ones a `.ghx` (the same tree as XML) names in its
`type_code` attribute. An unknown code is an error that says where, rather than
a silent desync that would garble everything after it.

Pure Python, no Rhino and no build123d: this is what the importer and the tests
run on. Geometry carried inside the archive (`ON_Data`) is decoded separately,
by `geometry.py`, and only that part needs rhino3dm.
"""
from __future__ import annotations

import struct
import uuid
import zlib
from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass
class Chunk:
    name: str
    index: int = -1
    items: list[tuple[str, int, int, Any]] = field(default_factory=list)
    chunks: list["Chunk"] = field(default_factory=list)

    def item(self, name: str, index: int = -1, default: Any = None) -> Any:
        for n, i, _t, v in self.items:
            if n == name and (index == -1 or i == index):
                return v
        return default

    def all(self, name: str) -> list[Any]:
        """Every value of an indexed item (`Source[0]`, `Source[1]`…), in order."""
        return [v for n, _i, _t, v in self.items if n == name]

    def sub(self, name: str, index: int = -1) -> "Chunk | None":
        for c in self.chunks:
            if c.name == name and (index == -1 or c.index == index):
                return c
        return None

    def subs(self, name: str) -> list["Chunk"]:
        return [c for c in self.chunks if c.name == name]

    def walk(self) -> Iterator["Chunk"]:
        yield self
        for c in self.chunks:
            yield from c.walk()


class ArchiveError(ValueError):
    pass


class _Reader:
    def __init__(self, data: bytes):
        self.b, self.p = data, 0

    def take(self, n: int) -> bytes:
        v = self.b[self.p:self.p + n]
        if len(v) < n:
            raise ArchiveError(f"archive truncated at byte {self.p}")
        self.p += n
        return v

    def i32(self) -> int:
        return struct.unpack("<i", self.take(4))[0]

    def string(self) -> str:
        n = shift = 0
        while True:
            c = self.take(1)[0]
            n |= (c & 0x7F) << shift
            shift += 7
            if not c & 0x80:
                break
        return self.take(n).decode("utf-8", "replace")

    def f64s(self, n: int) -> list[float]:
        return list(struct.unpack(f"<{n}d", self.take(8 * n)))

    def f32s(self, n: int) -> list[float]:
        return list(struct.unpack(f"<{n}f", self.take(4 * n)))

    def i32s(self, n: int) -> list[int]:
        return list(struct.unpack(f"<{n}i", self.take(4 * n)))


# GH_Types: type code -> how to read the value. Fixed-size tuples are lists.
def _value(r: _Reader, code: int) -> Any:
    if code == 1:  return bool(r.take(1)[0])              # bool
    if code == 2:  return r.take(1)[0]                    # byte
    if code == 3:  return r.i32()                         # int32
    if code == 4:  return struct.unpack("<q", r.take(8))[0]   # int64
    if code == 5:  return r.f32s(1)[0]                    # single
    if code == 6:  return r.f64s(1)[0]                    # double
    if code == 7:  return r.take(16).hex()                # decimal (kept raw)
    if code == 8:  return struct.unpack("<q", r.take(8))[0]   # date (ticks)
    if code == 9:  return str(uuid.UUID(bytes_le=r.take(16)))  # guid
    if code == 10: return r.string()                      # string
    if code in (20, 37):                                  # byte array / bitmap
        return r.take(r.i32())
    if code == 21: return r.f64s(r.i32())                 # double array
    if code in (30, 32): return r.i32s(2)                 # point / size
    if code in (31, 33): return r.f32s(2)                 # pointf / sizef
    if code == 34: return r.i32s(4)                       # rectangle
    if code == 35: return r.f32s(4)                       # rectanglef
    if code == 36: return r.i32()                         # color, ARGB
    if code == 50: return r.f64s(2)                       # point2d
    if code == 51: return r.f64s(3)                       # point3d
    if code == 52: return r.f64s(4)                       # point4d
    if code == 60: return r.f64s(2)                       # interval1d
    if code == 61: return r.f64s(4)                       # interval2d
    if code == 70: return r.f64s(6)                       # line
    if code == 71: return r.f64s(6)                       # bounding box
    if code == 72: return r.f64s(9)                       # plane: origin, x, y
    if code == 80: return r.i32s(3)                       # version
    raise ArchiveError(f"unknown GH_IO type code {code} at byte {r.p}")


def _chunk(r: _Reader) -> Chunk:
    c = Chunk(r.string(), r.i32())
    n_items, n_chunks = r.i32(), r.i32()
    for _ in range(n_items):
        name, index, code = r.string(), r.i32(), r.i32()
        c.items.append((name, index, code, _value(r, code)))
    for _ in range(n_chunks):
        c.chunks.append(_chunk(r))
    return c


def parse(raw: bytes) -> Chunk:
    """The root chunk of a `.gh` file's bytes (compressed or already inflated)."""
    data = raw
    if not raw.startswith(b"\x04Root"):
        try:
            data = zlib.decompressobj(-15).decompress(raw)
        except zlib.error as e:
            raise ArchiveError(f"not a Grasshopper .gh archive ({e})") from None
    if not data.startswith(b"\x04Root"):
        raise ArchiveError("not a Grasshopper .gh archive (no Root chunk)")
    r = _Reader(data)
    root = _chunk(r)
    if r.p != len(data):
        raise ArchiveError(f"trailing data: parsed {r.p} of {len(data)} bytes")
    return root


def load(path) -> Chunk:
    with open(path, "rb") as f:
        return parse(f.read())
