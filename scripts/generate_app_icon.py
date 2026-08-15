from __future__ import annotations

import struct
from pathlib import Path

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QRectF
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "bongo" / "assets"
SVG_PATH = ASSETS / "app-icon.svg"
PNG_PATH = ASSETS / "app-icon.png"
ICO_PATH = ASSETS / "app-icon.ico"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def render_png(renderer: QSvgRenderer, size: int) -> bytes:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()

    encoded = QByteArray()
    buffer = QBuffer(encoded)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError(f"Unable to encode {size}px icon")
    return bytes(encoded)


def write_ico(frames: list[tuple[int, bytes]]) -> None:
    header_size = 6 + 16 * len(frames)
    offset = header_size
    entries = []
    payload = []
    for size, png in frames:
        dimension = 0 if size >= 256 else size
        entries.append(
            struct.pack(
                "<BBBBHHII",
                dimension,
                dimension,
                0,
                0,
                1,
                32,
                len(png),
                offset,
            )
        )
        payload.append(png)
        offset += len(png)
    ICO_PATH.write_bytes(
        struct.pack("<HHH", 0, 1, len(frames)) + b"".join(entries) + b"".join(payload)
    )


def main() -> None:
    renderer = QSvgRenderer(str(SVG_PATH))
    if not renderer.isValid():
        raise RuntimeError(f"Invalid SVG: {SVG_PATH}")
    frames = [(size, render_png(renderer, size)) for size in ICON_SIZES]
    PNG_PATH.write_bytes(render_png(renderer, 1024))
    write_ico(frames)


if __name__ == "__main__":
    main()
