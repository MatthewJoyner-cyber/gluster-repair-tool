# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Read back selected backend evidence before any planned repair dispatch.

This detects changed observations, not a lock against concurrent clients. Keep
clients quiescent during repairs and retain per-step outcome verification.
"""
from __future__ import annotations

from .apply_binding import BindingError, _check_apply, _read_source
from .install_paths import DEFAULT_RESOLVER_PATH, DEFAULT_WORKER_PATH
from .models import ResolutionObservation
from .protocol import ResolveBatchRequest


def _fail(message: str) -> None:
    raise BindingError(f'live evidence changed or unavailable: {message}; rebuild evidence, plan and apply artifacts')


def _projection(item: dict) -> dict:
    # Mount lookups can trigger native heal. Recheck backend facts without a
    # mount probe; mount observation differences are not backend differences.
    values = {field: item.get(field, default) for field, default in
              ResolutionObservation(host='', raw_entry='').to_dict().items()
              if field not in {'host', 'depth', 'mounted'} and not field.startswith('mounted_')}
    if values.get('error'):
        _fail('resolver reported an error')
    if values.get('backend_lexists'):
        for field in ('backend_ctime_ns', 'backend_mtime_ns', 'backend_inode'):
            if not isinstance(values.get(field), int):
                _fail('saved/current backend identity lacks precise timestamps or inode')
    values['backend_child_names'] = sorted(values['backend_child_names'])
    return values


def compare_observations(saved: list[dict], current: list[ResolutionObservation], host: str) -> None:
    def keyed(items):
        result = {}
        for item in items:
            key = item.get('raw_entry')
            if not isinstance(key, str) or not key or key in result:
                _fail(f'missing or duplicate observation on {host}')
            result[key] = _projection(item)
        return result
    if keyed(saved) != keyed([item.to_dict() for item in current]):
        _fail(f'backend observation differs on {host}')


def validate_live_evidence(payload: dict, results: list, *, ssh_user: str) -> None:
    selected = [item for item in results if any(step.command_preview for step in item.steps)]
    if not selected:
        return
    from .manager import _run_resolve_worker
    binding = _check_apply(payload)
    evidence = _read_source(binding['sources']['evidence'])
    objects = evidence.get('objects')
    if not isinstance(objects, dict):
        _fail('missing object evidence')
    origin = binding['origin']
    bricks = {brick['host']: brick['path'] for brick in origin['bricks']}
    if len(bricks) != len(origin['bricks']):
        _fail('multiple bricks per host require unambiguous per-brick evidence')
    saved_by_host: dict[str, dict[str, dict]] = {}
    for result in selected:
        key = result.logical_path.lstrip('/')
        matches = [obj for path, obj in objects.items() if path.lstrip('/') == key]
        if len(matches) != 1 or not isinstance(matches[0], dict):
            _fail('selected action lacks unambiguous object evidence')
        observations = matches[0].get('observations')
        if not isinstance(observations, dict) or not observations:
            _fail('selected object has no backend observations')
        if set(observations) != set(bricks):
            _fail('selected object lacks observations for every bound brick')
        for host, saved in observations.items():
            if host not in bricks or not isinstance(saved, list) or not saved:
                _fail('observation host is outside the bound topology or lacks evidence')
            target = saved_by_host.setdefault(host, {})
            for observation in saved:
                if not isinstance(observation, dict):
                    _fail('invalid saved observation')
                raw = observation.get('raw_entry')
                if not isinstance(raw, str) or not raw:
                    _fail('saved observation has no raw entry')
                if raw in target and target[raw] != observation:
                    _fail('conflicting saved observations')
                target[raw] = observation
    for host, saved in saved_by_host.items():
        request = ResolveBatchRequest(volume=origin['volume'], brick_path=bricks[host],
                                      mountpoint='/' + origin['volume'],
                                      resolver_path=str(DEFAULT_RESOLVER_PATH),
                                      entries=sorted(saved), probe_mount=False)
        current = _run_resolve_worker(host=host, request=request,
                                      worker_path=str(DEFAULT_WORKER_PATH),
                                      ssh_user=ssh_user, local_aliases=set())
        compare_observations(list(saved.values()), current, host)
