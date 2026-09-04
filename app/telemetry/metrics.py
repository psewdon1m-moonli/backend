from __future__ import annotations

import re
import threading
from collections import defaultdict

METRIC_NAME = re.compile(r"[^a-zA-Z0-9_:]")


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._observations: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, int]] = {}

    @staticmethod
    def _labels(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((key, value.replace('"', "")) for key, value in labels.items()))

    def increment(self, name: str, value: float = 1, **labels: str) -> None:
        with self._lock:
            self._counters[(name, self._labels(labels))] += value

    def observe(self, name: str, value: float, **labels: str) -> None:
        key = (name, self._labels(labels))
        with self._lock:
            total, count = self._observations.get(key, (0.0, 0))
            self._observations[key] = (total + value, count + 1)

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            counters = list(self._counters.items())
            observations = list(self._observations.items())
        for (name, labels), value in sorted(counters):
            lines.append(f"{self._format(name, labels)} {value:g}")
        for (name, labels), (total, count) in sorted(observations):
            lines.append(f"{self._format(name + '_seconds_sum', labels)} {total:g}")
            lines.append(f"{self._format(name + '_seconds_count', labels)} {count}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _format(name: str, labels: tuple[tuple[str, str], ...]) -> str:
        safe_name = METRIC_NAME.sub("_", name)
        if not labels:
            return safe_name
        rendered = ",".join(f'{key}="{value}"' for key, value in labels)
        return f"{safe_name}{{{rendered}}}"
