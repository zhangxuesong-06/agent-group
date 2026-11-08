from __future__ import annotations

import json
from collections import Counter
from typing import Any

from .store import Store


DOMAINS = [
    "research",
    "analysis",
    "writing",
    "review",
    "general",
    "frontend",
    "backend",
    "devops",
    "code",
]

SCORE_FALLBACKS = {
    "frontend": ["frontend", "code", "general"],
    "backend": ["backend", "code", "general"],
    "devops": ["devops", "backend", "code", "general"],
    "code": ["code", "backend", "frontend", "devops", "general"],
}


def parse_scores(raw: str | dict[str, Any] | None) -> dict[str, float]:
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError):
            data = {}
    if not isinstance(data, dict):
        return {}
    scores: dict[str, float] = {}
    for key, value in data.items():
        if key not in DOMAINS:
            continue
        try:
            scores[key] = max(0.0, min(10.0, float(value)))
        except (TypeError, ValueError):
            continue
    return scores


class ModelRouter:
    """Pick the best enabled model for a task's capability.

    Score comes from per-provider domain ratings (0-10). Load is subtracted so
    concurrent runs do not pile onto the same model.
    """

    def __init__(self, store: Store) -> None:
        self.store = store

    def score_for(self, provider: dict[str, Any], capability: str) -> float:
        scores = parse_scores(provider.get("scores"))
        for key in SCORE_FALLBACKS.get(capability, [capability, "general"]):
            if key in scores:
                return scores[key]
        return 5.0

    def candidates(
        self, capability: str, allowed_ids: set[str] | None = None
    ) -> list[dict[str, Any]]:
        rows = self.store.get_enabled_providers()
        if allowed_ids is not None:
            rows = [provider for provider in rows if provider["id"] in allowed_ids]
        if not rows:
            return []
        scored: list[tuple[float, dict[str, Any]]] = []
        for provider in rows:
            scores = parse_scores(provider.get("scores"))
            if capability in scores and scores[capability] <= 0:
                continue
            score = self.score_for(provider, capability)
            if score <= 0:
                continue
            scored.append((score, provider))
        if not scored:
            # A 0 score means "avoid", not "break the run".
            scored = [(max(0.1, self.score_for(p, "general")), p) for p in rows]
        scored.sort(key=lambda item: -item[0])
        return [provider for _, provider in scored]

    def pick(
        self,
        capability: str,
        load: Counter[str] | dict[str, int],
        allowed_ids: set[str] | None = None,
    ) -> dict[str, Any] | None:
        candidates = self.candidates(capability, allowed_ids)
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda p: self.score_for(p, capability) - 0.8 * load.get(p["id"], 0),
        )

    def default(self) -> dict[str, Any] | None:
        return self.store.get_active_provider()
