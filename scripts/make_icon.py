"""Draw `scripts/agent-workbench.ico`, the icon `scripts/shortcut.cmd` hangs on
the desktop shortcut.

The `.ico` is committed rather than built on demand -- `shortcut.cmd` runs on a
machine that has Docker Desktop and nothing else, so it cannot render anything.
This file is committed beside it for the opposite reason: a binary asset whose
source is "somebody drew it once" is the kind of thing nobody dares change.
Here the mark is six numbers, and changing it is editing them.

Three decisions:

**Standard library only, like `scripts/architecture_panel.py`.** Pillow is in
the `computer-use` extra and nowhere else, so importing it would make redrawing
an icon depend on a multi-hundred-megabyte install that has nothing to do with
icons. The geometry is two shapes; the rasteriser below is twenty lines.

**Every entry is a PNG, including the small ones.** The ICO container also
allows a bottom-up BGRA DIB with a separate 1-bit mask, which is what the format
originally was and what most of the surviving documentation describes. Windows
reads PNG entries at every size since Vista, and Docker Desktop's own floor is
Windows 10 -- so the older encoding would buy compatibility with nothing that
can run this stack, in exchange for hand-writing a mask whose padding rules are
the classic way to produce an icon with a black box around it.

**The mark is the console's own: the rail's rounded tile with an A on it.**
Same two colours, same corner radius, so the icon on the taskbar and the mark at
the top of the sidebar are recognisably one thing. It is drawn from half-planes
rather than set in a font, and that is not a shortcut -- see `_in_mark`.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

#: Every size Windows asks for, from a list view (16) through the Start menu
#: and 200% displays to the preview pane (256). A size that is absent is not
#: missing -- the shell scales a neighbour -- but a scaled 32 next to a native
#: one is visibly softer, and these cost 5 KB in total.
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

#: Samples per axis inside one output pixel. The whole antialiasing story: the
#: shapes are tested at 64 points per pixel and the coverage becomes the alpha.
SUPERSAMPLE = 8

#: `--aw-nav-text` on `--aw-nav-bg` from `web/src/styles/tokens.css`, which is
#: what `.aw-logo-mark` paints: the tile takes the *text* colour and the letter
#: takes the *background* one, so the mark is an inversion of the rail around it.
#:
#: The dark-theme half of both tokens, deliberately. An icon cannot follow a
#: theme -- it is one image, on whatever wallpaper the machine has -- so one of
#: the two has to be chosen, and this is the pale tile with a near-black A that
#: the console shows on the dark rail people run it on.
GROUND = (0xE4, 0xE4, 0xDF)
MARK = (0x19, 0x19, 0x18)

#: 8px on a 28px tile, straight out of `.aw-logo-mark`, kept as the ratio so
#: that every size here rounds it the same way.
RADIUS = 8.0 / 28.0

#: The A, as five numbers in the unit square. Drawn rather than set in a font,
#: and that is forced rather than lazy: `--aw-display` resolves to
#: `var(--aw-sans)`, the *system* UI stack, so the rail's letter is Segoe UI on
#: Windows and SF on macOS. There is no one outline to copy -- only a shape.
#:
#: Bigger and heavier than the CSS says (14px type in a 28px tile, weight 600).
#: A desktop icon is read at 16 pixels in a list view, where that ratio puts the
#: crossbar under one pixel and the counter closes up into a blot. These
#: proportions are what survived being looked at down there.
APEX = 0.5
CAP_TOP = 0.245
BASELINE = 0.755
STROKE = 0.095

#: Half the width of the letter at the baseline, and the crossbar's band. The
#: bar sits low, as it does in the UI faces this is standing in for: high enough
#: to leave an open counter above it, low enough to leave a gap below.
HALF_WIDTH = 0.250
BAR_TOP = 0.560
BAR_BOTTOM = 0.655

#: How fast each leg leans away from the apex, in x per y.
_SLOPE = HALF_WIDTH / (BASELINE - CAP_TOP)


def _in_round_rect(x: float, y: float) -> bool:
    """Inside the full-bleed rounded square?

    One test rather than four corner cases: clamping the point into the inner
    rectangle whose corners are the arc centres makes every corner the same
    circle test, and every edge a distance of zero along one axis.
    """
    cx = min(max(x, RADIUS), 1.0 - RADIUS)
    cy = min(max(y, RADIUS), 1.0 - RADIUS)
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= RADIUS * RADIUS


def _in_mark(x: float, y: float) -> bool:
    """Inside the letter?

    An A is a triangle with two pieces taken out of it, and every edge involved
    is a straight line, so the whole glyph is four comparisons and no outline
    data: inside the outer triangle, then outside the inner one -- except where
    the crossbar crosses it.
    """
    if not (CAP_TOP <= y <= BASELINE):
        return False
    spread = _SLOPE * (y - CAP_TOP)
    if not (APEX - spread <= x <= APEX + spread):
        return False
    # The inner triangle is the outer one with a leg's width taken off each
    # side, so it opens up only below the height where the legs have leaned far
    # enough apart to have room between them. That is where the counter starts,
    # and it is a consequence of the stroke width rather than a sixth number.
    if not (APEX - spread + STROKE < x < APEX + spread - STROKE):
        return True
    return BAR_TOP <= y <= BAR_BOTTOM


def render(size: int) -> bytes:
    """Raw PNG scanlines: filter byte 0, then RGBA, top-down.

    The mark is composited against the ground *inside* the pixel rather than
    drawn over it afterwards, so a pixel that is half ground and half mark
    carries one blended colour at the ground's own alpha. Drawing it the other
    way leaves the mark's edge pixels partly transparent, and on a dark desktop
    background the A then has a dark rim.
    """
    hi = size * SUPERSAMPLE
    step = 1.0 / hi
    samples = SUPERSAMPLE * SUPERSAMPLE
    out = bytearray()
    for py in range(size):
        out.append(0)
        for px in range(size):
            ground = 0
            mark = 0
            for sy in range(SUPERSAMPLE):
                y = (py * SUPERSAMPLE + sy + 0.5) * step
                for sx in range(SUPERSAMPLE):
                    x = (px * SUPERSAMPLE + sx + 0.5) * step
                    if _in_round_rect(x, y):
                        ground += 1
                        if _in_mark(x, y):
                            mark += 1
            if ground == 0:
                out += bytes(4)
                continue
            t = mark / ground
            out += bytes(
                (
                    round(GROUND[0] * (1 - t) + MARK[0] * t),
                    round(GROUND[1] * (1 - t) + MARK[1] * t),
                    round(GROUND[2] * (1 - t) + MARK[2] * t),
                    round(255 * ground / samples),
                )
            )
    return bytes(out)


def _chunk(tag: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)


def png(size: int) -> bytes:
    """A minimal 8-bit RGBA PNG: signature, IHDR, one IDAT, IEND."""
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(render(size), 9))
        + _chunk(b"IEND", b"")
    )


def ico(sizes: tuple[int, ...] = SIZES) -> bytes:
    """Pack the PNGs into an ICO directory.

    A 256-pixel image records its width and height as 0: the fields are one
    byte each, so 256 does not fit, and 0 is how the format has always spelled
    it. Writing 256 there truncates to 0 anyway on a `<B`, but only after
    `struct.error` -- and an icon that fails to pack is better than one whose
    largest entry silently claims to be zero pixels wide for a different reason.
    """
    images = [png(size) for size in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    directory = bytearray()
    offset = len(header) + 16 * len(images)
    for size, data in zip(sizes, images, strict=True):
        side = 0 if size >= 256 else size
        directory += struct.pack(
            "<BBBBHHII", side, side, 0, 0, 1, 32, len(data), offset
        )
        offset += len(data)
    return header + bytes(directory) + b"".join(images)


def main() -> None:
    target = Path(__file__).resolve().parent / "agent-workbench.ico"
    target.write_bytes(ico())
    print(f"wrote {target} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
