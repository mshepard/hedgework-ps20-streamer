"""MJPEG ``multipart/x-mixed-replace`` framing helpers.

The browser's native ``<img>`` tag consumes responses with this content
type as a live image, swapping the displayed frame each time a new
``--<BOUNDARY>``-delimited part lands. We use a static boundary string
known on both sides and emit ``Content-Type`` / ``Content-Length``
headers on every part so picky proxies and the Chromium HTML parser
both stay happy.

The encode path runs on the default asyncio thread pool — PIL's JPEG
encoder releases the GIL during compression, so we don't need a
dedicated executor here.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image


BOUNDARY = "frame"
CONTENT_TYPE = f"multipart/x-mixed-replace; boundary={BOUNDARY}"


def encode_jpeg(array_rgb: np.ndarray, quality: int) -> bytes:
    """Encode an ``(H, W, 3)`` RGB ndarray as a JPEG byte string."""

    image = Image.fromarray(array_rgb, mode="RGB")
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality, optimize=False)
    return buf.getvalue()


def part(jpeg: bytes) -> bytes:
    """Wrap one JPEG frame in a multipart part suitable for streaming.

    The leading CRLF + boundary follows the form most browsers and
    middleboxes expect (``\\r\\n--<BOUNDARY>\\r\\n``). ``Content-Length``
    is included so flush behaviour is well-defined even when the
    underlying connection is slow.
    """

    header = (
        b"\r\n--"
        + BOUNDARY.encode("ascii")
        + b"\r\n"
        + b"Content-Type: image/jpeg\r\n"
        + f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii")
    )
    return header + jpeg
