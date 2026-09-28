# Gluster recovery limits and how this tool helps

GlusterFS combines storage bricks into a volume. In replicated layouts, its
Automatic File Replication (AFR) translator tracks changes and determines which
replicas can supply a heal. Native healing handles recoverable divergence;
an external repair tool should preserve that mechanism rather than assume every
pending entry needs manual intervention.
[Gluster replication documentation](https://docs.gluster.org/en/main/Administrator-Guide/Automatic-File-Replication/)

## Why an apparently healthy cluster can still have a problem

A brick process being online does not establish that every client and self-heal
daemon can reach it. Healing also depends on usable source information.
Troubleshooting therefore spans client, brick, self-heal, and heal-command logs.
Large backlogs can make heal inspection slow; backlog counts and detailed
object diagnosis serve different purposes.
[Self-heal troubleshooting](https://docs.gluster.org/en/main/Troubleshooting/troubleshooting-afr/)

An outage or restart is a reason to inspect current connectivity and evidence,
not enough information to diagnose split-brain. This project's historical
incidents do not establish that ordinary controlled upgrades inherently corrupt
Gluster volumes.

## Where recovery becomes difficult

| Problem | Recovery difficulty | Tool contribution |
| --- | --- | --- |
| Pending native heal | Need connectivity and a usable source | Health, heal snapshots, evidence summaries |
| Data split-brain | Replicas disagree on content | Compare evidence and expose explicit source decisions |
| Metadata split-brain | Permissions, ownership, or other metadata disagree | Collect metadata and distinguish source choice from harmless drift |
| Entry/GFID or type conflict | The same name refers to different objects or types | Group identities and bound directory comparison |
| Recurring index/handle residue | A marker alone does not prove an object is dead | Follow references and retain ambiguous cases for review |
| Missing child or parent chain | A visible parent does not prove a child should be recreated | Plan concrete dependencies and follow-up evidence |

Gluster documents data, metadata, and entry split-brain separately. Its native
resolution commands offer choices such as an explicit source brick, but type
mismatch is outside those documented split-brain resolution methods. Selecting
a source is a statement about which version to preserve, not just a way to
silence an error.
[Split-brain resolution](https://docs.gluster.org/en/main/Troubleshooting/resolving-splitbrain/)

The reference-chain, dependency, and residue conclusions above come from this
tool's implementation work; see the [sanitized history](../HISTORY.md). They
are not a claim that every GFID-only row or I/O error has the same cause.

## Quorum and arbiters

Quorum controls which operations are permitted when replicas are unavailable.
It does not provide the missing file content or decide application intent.
Client quorum and server quorum have different roles and should not be
interchanged in diagnosis.

An arbiter configuration adds metadata-based arbitration while storing payload
on the data bricks. The arbiter is never a recovery source for file bytes.
Its presence does not make backend edits or unsupported recovery procedures safe.
[Arbiter and quorum documentation](https://docs.gluster.org/en/main/Administrator-Guide/arbiter-volumes-and-quorum/)

## Why evidence collection needs care

AFR records pending operations and uses them during source/sink selection.
Client access can initiate healing as well as observe an object. A probe may
therefore change the condition being investigated. Capture baseline evidence,
keep probing narrow, and distinguish observation from an intervention.
[Replication and healing](https://docs.gluster.org/en/main/Administrator-Guide/Automatic-File-Replication/)

Use the appropriate client, brick, self-heal, and glfsheal logs together when
a result is unclear. Temporarily increased logging needs an explicit scope and
restoration plan. An empty heal listing does not substitute for checking the
specific user-visible failure.
[Self-heal troubleshooting](https://docs.gluster.org/en/main/Troubleshooting/troubleshooting-afr/)

## What the repair tool adds

The tool converts raw evidence into logical objects, classifies repair and
review cases, builds explicit apply recipes, and presents operator decisions.
It provides bounded directory comparisons, preservation operations, verification,
and run artifacts so an investigation can explain why an action was proposed.

The intended flow is evidence, classification, plan, authorization, execution,
verification, then fresh evidence. If evidence changes or a write may have
completed without a known result, stop the old plan and establish what happened.

It cannot recover an authoritative version that no longer exists, infer the
user's desired contents from timestamps alone, repair failing storage/network
infrastructure, or replace independent backups. A larger/newer file and an
accessible replica are evidence, not universal winner rules.

## Current limits

This is a public beta with a narrow live repair claim: one supervised nonempty
missing-replica file restore on the listed Ubuntu 24.04 / Gluster 11.1 lab.
Other repair recipes remain experimental. Tool-driven full namespace heal and
native per-file resolver writes are disabled. See the [release notes](RELEASE_NOTES_v0.1.0-beta.3.md),
[validation](VALIDATION.md) and [compatibility profile](GLUSTER_COMPATIBILITY.md)
before a write. Neither the core nor the optional companion is an unattended
healer.

Native healing and preserved evidence should guide the next action. Where
source authority or reversibility is unresolved, retain the review case and
seek the missing evidence rather than repeatedly deleting residue.
