"""dB <-> mcp_logic mixer contract (0..1).

`logic_mixer.set_volume` takes a *contract* value, not dB and not linear amplitude:
mcp_logic maps it through a measured taper (AXValueExtractors.logicMixerFaderContractToPosition)
and reports the resulting level in dB through the `logic://mixer` resource. There is no
dB→contract law in the server, so the adapter refuses to invent one: it interpolates a
calibration measured on the live session (see `adapter/calibrate_fader.py`).
"""
from __future__ import annotations

import json
import os


class FaderCalibration:
    def __init__(self, points: list[list[float]] | None = None):
        self.points = sorted((float(d), float(c)) for d, c in (points or []))

    @classmethod
    def load(cls, path: str | None) -> "FaderCalibration":
        if not path or not os.path.isfile(path):
            return cls([])
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return cls(data.get("points", []))

    @property
    def complete(self) -> bool:
        return len(self.points) >= 3

    def to_contract(self, db: float) -> float | None:
        """Interpolate dB -> contract. None when the calibration cannot cover it."""
        if not self.complete:
            return None
        if db <= self.points[0][0]:
            return self.points[0][1]
        if db >= self.points[-1][0]:
            return self.points[-1][1]
        for (d0, c0), (d1, c1) in zip(self.points, self.points[1:]):
            if d0 <= db <= d1:
                if d1 == d0:
                    return c0
                ratio = (db - d0) / (d1 - d0)
                return round(c0 + ratio * (c1 - c0), 4)
        return None

    def describe(self) -> dict:
        return {"points": len(self.points), "complete": self.complete,
                "range_db": [self.points[0][0], self.points[-1][0]] if self.points else None}
