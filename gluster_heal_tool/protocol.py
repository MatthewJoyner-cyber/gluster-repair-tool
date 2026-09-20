# SPDX-License-Identifier: GPL-2.0-only
"""Volume resolution protocol helpers."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .models import ResolutionObservation
from .shared_io import write_json_shared


@dataclass
class ResolveBatchRequest:
    volume: str
    brick_path: str
    mountpoint: str
    resolver_path: str
    entries: list[str]
    verbose: bool = False
    probe_mount: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChecksumTarget:
    logical_path: str
    backend_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChecksumBatchRequest:
    items: list[ChecksumTarget]

    def to_dict(self) -> dict[str, Any]:
        return {"items": [item.to_dict() for item in self.items]}


@dataclass
class CanaryBatchRequest:
    volume: str
    scenario: str
    backend_root: str
    ops: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    write_json_shared(path, payload)


def read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def observations_to_payload(observations: list[ResolutionObservation]) -> dict[str, Any]:
    return {"observations": [item.to_dict() for item in observations]}


def observations_from_payload(payload: dict[str, Any]) -> list[ResolutionObservation]:
    return [ResolutionObservation(**item) for item in payload.get("observations", [])]
