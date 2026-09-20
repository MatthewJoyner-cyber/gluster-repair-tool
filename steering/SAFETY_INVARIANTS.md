# Safety Invariants

This document defines the safety properties that every discovery, planner,
apply, executor, resume, and operator-interface change must preserve. It is
the review contract for this repository, not a claim that every Gluster fault
has an automatic repair.

## Authority And Evidence

1. A repair action is justified only by recorded evidence, an explicit planner
   rule, and the current topology and evidence fingerprints.
2. `safe_default` means the current evidence proves a particular narrow action
   is justified. It does not merely mean that an action looks low risk.
3. An operator decision is evidence-bound state, not a permanent preference.
   A topology, identity, content, metadata, or evidence-fingerprint change
   invalidates the decision until it is reviewed again.
4. Ambiguity may stop automation, but it must not stop diagnosis. The next edge
   must be bounded evidence gathering, reversible preservation, native heal,
   an explicit operator choice, or a support handoff.
5. Ambiguous user data is preserved before authority is invented. In particular,
   the tool must not silently choose a split-brain source, infer a no-majority
   winner, or use an arbiter as a file-payload source.

## Operator Default And Decision Contract

The normal operator path must be as simple as the evidence permits. A
reversible, evidence-backed action may be `safe_default` when the plan gives
its exact scope and a practical narrow rollback. The final report must plainly
show that this path ran, what changed, the verification result, remaining
caveats, and the exact revert path.

Material uncertainty, source authority, meaningful data-risk, incomplete
rollback, or a policy choice requires a planner-owned choice card. It must show
the relevant evidence, affected scope, proposed alternatives, recommendation,
risk, protection/rollback, and a no-change skip.

A review-only outcome is acceptable only when the tool genuinely lacks a safe
next repair edge. It must say why, preserve the evidence, and provide a bounded
next diagnostic step or a concrete support-case handoff; it must never be a
terminal vague review.

## Planner And Executor Boundary

> The planner may infer what is justified. The executor may not infer anything.

The planner may classify evidence, choose a policy-backed recommendation, build
dependency closure, calculate exact commands and targets, attach backup/revert
steps, and require an operator decision. The executor may only validate and
perform that already-materialized plan.

No write operation may exist unless it can be traced through this chain:

```text
recorded evidence -> planner rule -> apply plan -> required authorization
-> exact command -> recorded result -> verification
```

The executor must not select a source, widen scope, substitute a target, choose
quarantine, reinterpret an ambiguous result, or create a new repair policy.

## Execution Gaps And Replanning

An execution gap is any observation that means the approved plan no longer
matches reality: a changed topology or fingerprint, absent or changed source or
target, GFID mismatch, unavailable backup, unmet dependency, unexpected worker
result, or failed verification.

For a gap discovered before a write starts, the executor must:

1. stop the affected action and every dependent action;
2. record the failed precondition and the observed facts in run artifacts;
3. make no replacement choice and start no unrelated dependent write;
4. return control for fresh evidence and replanning.

For a gap discovered after a command may have started, the write state is
`unknown` until focused verification proves otherwise. The executor must stop
further dependent writes, preserve the command/result artifacts, and not retry
or recover by inference. The next write requires a planner-produced recovery
path based on fresh evidence and any required operator authorization.

The executor itself does not prompt the operator. In an interactive run, the
controller may tell the operator that execution stopped, then refresh the
original evidence route and render a planner-owned decision card. In a
non-interactive run, it must stop with an exact status/resume or support-handoff
path.

## Operator Line Of Evidence

Operator output and persisted artifacts should make this chain inspectable:

```text
observed evidence -> classification -> proposed action -> authorization
-> execution result -> verification -> fresh evidence -> revised plan
```

Decision cards, ready-batch previews, and execution reports must retain the
object scope, affected hosts, evidence provenance, planner rationale, exact
steps, backup/rollback coverage, command outcome, verification result, and the
next safe action. A review-only result is complete only when it names that next
action.

## Change Checklist

Any change that crosses manifest, planner, apply planning, executor, or resume
boundaries must state and test:

- the evidence predicate and provenance it relies on;
- the action safety class and authority boundary;
- exact targets, dependencies, backup/revert, and verification behavior;
- the pre-write mismatch outcome;
- the post-write-unknown outcome;
- stale-decision invalidation and replan behavior; and
- a concrete continuation for ambiguous or unsupported cases.

New repair logic is not complete until its invariants are represented in the
repair matrix and covered by focused regression tests. Live proof, when needed,
must name the evidence source, proof label, whether a write occurred, and the
final health/heal result.
