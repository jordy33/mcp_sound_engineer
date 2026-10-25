"""Channel EQ calibration: Logic exposes plugin parameters as raw AX values.

A calibration table maps musical units (Hz, dB, Q) to the raw value the bridge
writes, per parameter index. Build it once per Logic version and keep it in the
repo (or in ~/.config) — then EQ actions become appliable.
"""
from __future__ import annotations

import json
import os


class Calibration:
    def __init__(self, data: dict):
        self.data = data or {}

    @classmethod
    def load(cls, path: str) -> "Calibration":
        if not path or not os.path.isfile(path):
            return cls({})
        with open(path, encoding="utf-8") as handle:
            return cls(json.load(handle))

    # -- queries ---------------------------------------------------------
    @property
    def plugin(self) -> dict:
        return self.data.get("channel_eq", {})

    def param_index(self, name: str) -> int | None:
        index = self.plugin.get("param_index", {}).get(name)
        return int(index) if index is not None else None

    def to_raw(self, name: str, value: float) -> float | None:
        points = self.plugin.get("curves", {}).get(name)
        if not points:
            return None
        pairs = sorted((float(p[0]), float(p[1])) for p in points)
        if value <= pairs[0][0]:
            return pairs[0][1]
        if value >= pairs[-1][0]:
            return pairs[-1][1]
        for (x0, y0), (x1, y1) in zip(pairs, pairs[1:]):
            if x0 <= value <= x1:
                if x1 == x0:
                    return y0
                ratio = (value - x0) / (x1 - x0)
                return round(y0 + ratio * (y1 - y0), 4)
        return None

    def covers(self, names: list[str]) -> bool:
        return all(self.param_index(n) is not None and self.to_raw(n, 1.0) is not None
                   for n in names)

    def describe(self) -> dict:
        return {"plugin": "channel_eq",
                "parameters": sorted(self.plugin.get("param_index", {})),
                "curves": sorted(self.plugin.get("curves", {}))}
