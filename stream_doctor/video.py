from __future__ import annotations

from dataclasses import dataclass
import base64
from hashlib import sha256
from statistics import mean
from typing import Sequence

from PySide6.QtGui import QImage


@dataclass(frozen=True)
class FrameObservation:
    brightness: float
    black_ratio: float
    fingerprint: str
    is_black: bool
    is_frozen: bool


class FrameAnalyzer:
    """Analyze caller-provided pixels; it never writes or transmits frames."""
    def __init__(self, brightness_threshold: float = 8, black_ratio: float = 0.98, freeze_samples: int = 5):
        self.brightness_threshold = brightness_threshold
        self.black_ratio_threshold = black_ratio
        self.freeze_samples = freeze_samples
        self._previous: str | None = None
        self._same_count = 0

    def observe(self, pixels: Sequence[Sequence[int]]) -> FrameObservation:
        values = [int(channel) for pixel in pixels for channel in pixel]
        if not values:
            raise ValueError("frame has no pixels")
        brightness = mean(values)
        dark_values = [mean(pixel) for pixel in pixels]
        ratio = sum(value <= self.brightness_threshold for value in dark_values) / len(dark_values)
        fingerprint = sha256(bytes(max(0, min(255, value)) for value in values)).hexdigest()
        if fingerprint == self._previous:
            self._same_count += 1
        else:
            self._same_count = 1
            self._previous = fingerprint
        return FrameObservation(brightness, ratio, fingerprint, ratio >= self.black_ratio_threshold, self._same_count >= self.freeze_samples)


def analyze_png_data(image_data: str, analyzer: FrameAnalyzer) -> FrameObservation:
    raw = image_data.split(",", 1)[-1]
    image = QImage()
    if not image.loadFromData(base64.b64decode(raw)) or image.width() <= 0 or image.height() <= 0:
        raise ValueError("OBS returned an unreadable screenshot")
    pixels = [[image.pixelColor(x, y).red(), image.pixelColor(x, y).green(), image.pixelColor(x, y).blue()] for y in range(image.height()) for x in range(image.width())]
    return analyzer.observe(pixels)


def is_unexpected_freeze(observation: FrameObservation, source_name: str, intentional_static_sources: set[str]) -> bool:
    return observation.is_frozen and source_name not in intentional_static_sources and not observation.is_black
