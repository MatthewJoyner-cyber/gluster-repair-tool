# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Content-bound evidence lineage for saved execution artifacts.

These hashes detect changed or mixed artifacts, not a malicious author who can
rewrite and re-sign the whole bundle. They are not live inode/content checks or
a cluster lock; per-action evidence guards and post-write verification still apply.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from pathlib import Path

from .volume import normalize_host_alias, parse_bricks, parse_brick_roles


class BindingError(ValueError):
    """The saved execution authority no longer matches its inputs."""


def _fail(message: str) -> None:
    raise BindingError(f"apply origin binding: {message}; rebuild evidence, plan and apply artifacts")


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def volume_identity(volume_info: str) -> str:
    values = [line.split(':', 1)[1].strip() for line in volume_info.splitlines()
              if line.strip().startswith('Volume ID:')]
    if len(values) != 1:
        _fail('missing or ambiguous volume ID')
    try:
        identity = uuid.UUID(values[0])
    except (ValueError, AttributeError):
        _fail('invalid volume ID')
    if identity.int == 0:
        _fail('empty volume ID')
    return str(identity)


def _bricks(items: list[dict]) -> list[dict]:
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            _fail("invalid brick identity")
        host = normalize_host_alias(str(item.get("host") or ""))
        path = str(item.get("path") or "").rstrip("/")
        role = str(item.get("role") or "")
        if not host or not path.startswith("/") or role not in {"data", "arbiter"}:
            _fail("missing brick identity or role")
        normalized.append({"host": host, "path": path, "role": role})
    if not normalized or len({(b["host"], b["path"]) for b in normalized}) != len(normalized):
        _fail("missing or duplicate brick identities")
    return sorted(normalized, key=lambda b: (b["host"], b["path"], b["role"]))


def _fingerprint(payload: dict, binding: dict) -> str:
    return _digest({"payload": {k: v for k, v in payload.items() if k != "origin_binding"},
                    "binding": {k: v for k, v in binding.items() if k != "fingerprint"}})


def _seal(payload: dict, *, kind: str, origin: dict, sources: dict) -> None:
    binding = {"schema_version": 2, "kind": kind, "origin": copy.deepcopy(origin),
               "sources": copy.deepcopy(sources)}
    binding["fingerprint"] = _fingerprint(payload, binding)
    payload["origin_binding"] = binding


def _check(payload: dict, kind: str) -> dict:
    if not isinstance(payload, dict):
        _fail("invalid artifact payload")
    binding = payload.get("origin_binding")
    if not isinstance(binding, dict) or binding.get("schema_version") not in (1, 2) or binding.get("kind") != kind:
        _fail("missing or unsupported binding")
    origin = binding.get("origin")
    if not isinstance(origin, dict) or not isinstance(origin.get("volume"), str) or not origin["volume"].strip():
        _fail("missing origin volume")
    if not isinstance(origin.get("bricks"), list) or _bricks(origin["bricks"]) != origin["bricks"]:
        _fail("invalid origin topology")
    if binding.get("fingerprint") != _fingerprint(payload, binding):
        _fail(f"{kind} content or target identity changed")
    if not isinstance(binding.get("sources"), dict):
        _fail("missing source fingerprints")
    return binding


def _reference(path: str | Path, payload: dict) -> dict:
    return {"path": str(Path(path).expanduser().absolute()), "fingerprint": _digest(payload)}


def _read_source(reference: dict) -> dict:
    if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
        _fail("missing source artifact")
    try:
        payload = json.loads(Path(reference["path"]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _fail("source artifact is missing or unreadable")
    if not isinstance(payload, dict) or _digest(payload) != reference.get("fingerprint"):
        _fail("source evidence or plan changed")
    return payload


def bind_manifest(payload: dict, *, volume: str, bricks: list[dict], volume_id: str = "") -> None:
    """Only evidence collection supplies this origin; never recover it from status."""
    if not volume.strip():
        _fail("missing evidence volume")
    if not volume_id:
        return  # Evidence without a volume generation remains preview-only.
    identity = volume_identity('Volume ID: ' + volume_id)
    _seal(payload, kind="evidence", origin={"volume": volume.strip(), "volume_id": identity,
          "bricks": _bricks(bricks)}, sources={})


def bind_plan(payload: dict, manifest: dict, manifest_path: str | Path) -> None:
    if "origin_binding" not in manifest:
        return  # Legacy evidence remains useful for preview, never for execution.
    binding = _check(manifest, "evidence")
    _seal(payload, kind="plan", origin=binding["origin"],
          sources={"evidence": _reference(manifest_path, manifest)})


def _check_plan(plan: dict) -> dict:
    binding = _check(plan, "plan")
    evidence = _read_source(binding["sources"].get("evidence"))
    evidence_binding = _check(evidence, "evidence")
    if evidence_binding["origin"] != binding["origin"]:
        _fail("plan and evidence origins differ")
    return binding


def validate_plan_context(plan: dict, *, volume: str, brick_path: str = "") -> None:
    if "origin_binding" not in plan:
        return
    binding = _check_plan(plan)
    origin = binding["origin"]
    if volume.strip() != origin["volume"]:
        _fail("status volume differs from plan volume")
    if brick_path and brick_path.rstrip("/") not in {b["path"] for b in origin["bricks"]}:
        _fail("status brick path differs from evidence")


def bind_apply(payload: dict, plan: dict, plan_path: str | Path) -> None:
    if "origin_binding" not in plan:
        return
    binding = _check_plan(plan)
    _seal(payload, kind="apply", origin=binding["origin"],
          sources={**binding["sources"], "plan": _reference(plan_path, plan)})


def _check_apply(payload: dict) -> dict:
    binding = _check(payload, "apply")
    plan = _read_source(binding["sources"].get("plan"))
    plan_binding = _check_plan(plan)
    if (binding["origin"] != plan_binding["origin"] or
            binding["sources"].get("evidence") != plan_binding["sources"].get("evidence")):
        _fail("apply and plan origins differ")
    return binding


def derive_apply_binding(payload: dict, previous: dict) -> None:
    """Explicitly planned continuations inherit validated lineage, never status."""
    if "origin_binding" not in previous:
        return
    binding = _check_apply(previous)
    _seal(payload, kind="apply", origin=binding["origin"], sources=binding["sources"])


def validate_apply_binding(payload: dict, status: dict) -> str:
    binding = _check_apply(payload)
    origin = binding["origin"]
    if not origin.get('volume_id'):
        _fail('missing volume ID in saved evidence')
    volume_identity('Volume ID: ' + str(origin['volume_id']))
    if str(status.get("volume") or "").strip() != origin["volume"]:
        _fail("status volume differs from apply volume")
    for field, source in (("manifest_in", "evidence"), ("manifest_out", "evidence"), ("plan_out", "plan")):
        if status.get(field):
            reference = {**binding["sources"][source], "path": str(status[field])}
            _read_source(reference)
    return origin["volume"]


def validate_live_topology(payload: dict, volume_info: str) -> None:
    origin = _check(payload, "apply")["origin"]
    names = [line.split(":", 1)[1].strip() for line in volume_info.splitlines()
             if line.strip().startswith("Volume Name:")]
    if names != [origin["volume"]]:
        _fail("live volume identity differs from evidence")
    if volume_identity(volume_info) != origin.get('volume_id'):
        _fail('live volume ID differs from evidence')
    try:
        roles = parse_brick_roles(volume_info)
        live = _bricks([{"host": host, "path": path, "role": roles.get(host, "")}
                        for host, path in parse_bricks(volume_info)])
    except (RuntimeError, ValueError) as exc:
        _fail(f"live topology unavailable ({exc})")
    if live != origin["bricks"]:
        _fail("live brick topology differs from evidence")
