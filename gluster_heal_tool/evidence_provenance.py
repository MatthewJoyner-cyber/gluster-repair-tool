"""Evidence-source vocabulary and proof-scope helpers."""
from __future__ import annotations

from typing import Final

HEAL_INFO_INPUT_SOURCE: Final = "heal_info"
OPERATOR_PATH_INPUT_SOURCE: Final = "operator_path"
OPERATOR_GFID_INPUT_SOURCE: Final = "operator_gfid"
OPERATOR_GFID_CHILD_INPUT_SOURCE: Final = "operator_gfid_child"
OPERATOR_INDEX_INPUT_SOURCE: Final = "operator_index"
CANARY_STATE_INPUT_SOURCE: Final = "canary_state"

HARNESS_ONLY_PROOF_SCOPE: Final = "harness-only"

OPERATOR_DISCOVERABLE_INPUT_SOURCES: Final = frozenset(
    {
        HEAL_INFO_INPUT_SOURCE,
        OPERATOR_PATH_INPUT_SOURCE,
        OPERATOR_GFID_INPUT_SOURCE,
        OPERATOR_GFID_CHILD_INPUT_SOURCE,
        OPERATOR_INDEX_INPUT_SOURCE,
    }
)


def canary_state_plan_provenance(
    *,
    kind: str,
    volume: str,
    scenario: str,
) -> dict[str, object]:
    """Build explicit non-production provenance for a state-bridge plan."""
    identity = {
        "kind": kind.strip(),
        "volume": volume.strip(),
        "scenario": scenario.strip(),
    }
    if not all(identity.values()):
        raise ValueError("canary state provenance requires kind, volume, and scenario")
    return {
        "input_source": CANARY_STATE_INPUT_SOURCE,
        "proof_scope": HARNESS_ONLY_PROOF_SCOPE,
        "canary": identity,
    }


def is_operator_discoverable_input_source(input_source: object) -> bool:
    return str(input_source or "").strip() in OPERATOR_DISCOVERABLE_INPUT_SOURCES
