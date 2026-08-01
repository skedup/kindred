from __future__ import annotations

import secrets
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

LOCATION_CANDIDATE_RESOLVER_KEY = "location.candidates"


class LocationCandidateResolver:
    def __init__(self) -> None:
        self._nonce = secrets.token_urlsafe(16)
        self._entries: dict[str, dict[str, Any]] = {}
        self._refs: dict[tuple[str, str], str] = {}

    def register(self, candidate: Mapping[str, Any]) -> str:
        canonical = deepcopy(dict(candidate))
        binding_id = canonical.get("binding_id")
        place_key = canonical.get("place_key")
        if not isinstance(binding_id, str) or not isinstance(place_key, str):
            raise TypeError("canonical candidate requires binding_id and place_key")
        key = (binding_id, place_key)
        existing_ref = self._refs.get(key)
        if existing_ref is not None:
            if self._entries[existing_ref] != canonical:
                raise ValueError("canonical candidate changed within one tool loop")
            return existing_ref
        candidate_ref = f"loc_{self._nonce}_{len(self._entries) + 1}"
        self._refs[key] = candidate_ref
        self._entries[candidate_ref] = canonical
        return candidate_ref

    def resolve(self, candidate_ref: str) -> dict[str, Any]:
        candidate = self._entries.get(candidate_ref)
        if candidate is None:
            raise KeyError("candidate_ref is invalid or expired")
        return deepcopy(candidate)
