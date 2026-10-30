from __future__ import annotations

import json
from pathlib import Path

from .state import TrialMemoryRecord


CORE_FIELDS = (
    "lane",
    "trial_index",
    "informal",
    "formal_state",
    "lean_error_log",
    "retry_plan",
)


class JsonlTrialMemory:
    def __init__(self, path: Path, lane: str = "single"):
        self.path = path
        self.lane = lane

    def append(self, record: TrialMemoryRecord) -> None:
        payload = dict(record)
        for field in CORE_FIELDS:
            payload.setdefault(field, "")
        payload["lane"] = payload["lane"] or self.lane
        payload["trial_index"] = payload["trial_index"] or self._next_trial_index()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def clear(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")

    def load_all(self) -> list[TrialMemoryRecord]:
        if not self.path.exists():
            return []
        records: list[TrialMemoryRecord] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                raw = json.loads(line)
                records.append(raw)
        return records

    def _next_trial_index(self) -> int:
        if not self.path.exists():
            return 1
        return sum(1 for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()) + 1
