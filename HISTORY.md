# Implementation history before the repository split

This is a frozen, sanitized summary of development before migration. It is not
a copy of the original ledger and does not import that repository's commits.
It preserves transferable findings from implementation, synthetic tests, and
bounded lab work. Exact inventories, incident identifiers, logs, artifacts,
personal notes, and private source references remain in the reference archive.

Future implementation details belong in each new commit. This summary's
acceptance statements are historical and limited; they are not a current
health report or a guarantee about every Gluster release.

## From individual heal lines to logical objects

Early scripts could resolve a heal entry and demonstrate a narrow
stage/delete/restore operation. Another row could still represent the same
filename through a different GFID. Repairing one line at a time was therefore
the wrong unit of work.

The Python implementation introduced a heal parser, cached/live observations,
a manifest keyed by logical object, and a separate dry-run planner. Multiple
GFIDs and heal aliases could be considered together. Parent/child relationships
became explicit rather than incidental ordering.

## From ad hoc transport to manager and worker

Per-entry SSH was chatty and difficult to audit. Discovery moved toward batched
host workers, volume-driven layout discovery, and structured artifacts. SSH
became transport rather than the place where repair decisions were made.

Health checks, status records, previews, and progress summaries made long runs
inspectable. Later work distinguished online bricks from attached self-heal
daemons and physical hosts from endpoint aliases. An online brick alone does
not establish that the healing path is ready.

## Directory repair became a separate problem

File winner selection did not translate safely to directory conflicts. The
implementation added bounded tree comparison, explicit conflict decisions,
quarantine choices, and dependency-aware planning.

A parent existing on enough replicas does not prove which missing children
should exist. Child-gap promotion was tightened to require concrete executable
child actions. Repairing an already-present parent is not a substitute for
resolving a missing child. Exact-half replica cohorts remain no-majority cases,
even when client quorum permits operations.

## GFID, hardlink, and stale-index discoveries

A GFID-only row does not prove an object is dead. A handle can still refer to a
live namespace entry through a hardlink or backlink. Discovery learned to
follow identity/reference chains before classifying dead-object cleanup.

Internal index entries and live file handles require different treatment.
Recurring bookkeeping after removal is a reason to refresh evidence and
reconsider classification, not to repeat deletion indefinitely. A narrow fix
can expose another layer of a directory or identity problem.

## Evidence sources and proof labels

Initial canary successes sometimes proved only setup and teardown: cleanup
consumed the same state that created the fault. This was insufficient evidence
that normal discovery could find and repair the problem.

Proof labels were separated into synthetic/harness coverage, diagnostic cases,
native-heal-first outcomes, operator-path mechanical alignment, and independently
observed repair acceptance. Focused path, GFID, child, and index entry points
were added so a known problem did not have to appear in heal output first.

Mount lookup can change what is being observed by invoking native healing.
Some injected metadata differences disappeared on lookup or crawl. Those runs
were reclassified as native-heal or diagnostic evidence, not repair-tool wins.

## POSIX metadata and native resolution

Mode, ownership, group, and ACL disagreements were separated from timestamp or
mdata-only drift. Source selection requires identity/payload agreement and
either a justified majority or an explicit operator choice.

A bounded three-data-replica metadata conflict had independent split-brain
evidence and successful native source selection. That result does not generalize
to every width or failure shape. A direct four-replica xattr/index tie did not
establish a durable protocol-level conflict and remains diagnostic-only.
Absence of a safe fixture control is a tooling gap, not proof that an experiment
is impossible.

## Arbiter and alias safety

Role-aware checks prevent an arbiter from supplying file payload. Metadata
evidence and quorum participation do not make an arbiter a third data copy.
Ambiguous aliases must not collapse distinct brick identities or votes.

Several bounded arbiter cases informed role and residue handling, but unresolved
data-plus-arbiter conflicts remain review/support cases. A classification label
is not a complete root-cause explanation. A later review found inconsistent
split-brain annotation between discovery routes; this is still a release issue.

## Human-guided repair and preservation

Guided repair introduced decision cards, visible skip/defer choices, preview,
explicit batch authorization, and separate source decisions. Resume and write
history were improved so a later read-only pass does not erase a prior write.

Archive restoration gained containment checks, destination validation,
conflict handling, host mapping, and dangling-symlink handling. These changes
improved local safety, but subsequent review found that remote archive staging
still loses metadata and hardlink fidelity. Completed earlier work must not
be described as complete end-to-end rollback assurance.

## Pre-migration review and remaining caveats

The full predecessor suite passed 699 tests, yet additional fault probes found
13 issues: seven high priority and six medium priority. Important gaps include
volume/evidence binding, a manager gate after execution, dependent actions
running after prerequisite failure, destructive fallback after resolver transport
errors, lost execution records on exceptions, remote backup fidelity, and
fresh bootstrap layout.

Other findings cover host-helper compatibility, legacy canary eligibility,
overbroad heal-path matching, evidence classification, ignored logging options,
and machine-specific defaults. Portable state defaults are addressed in this
candidate, but fresh-install and platform qualification remain incomplete.

The [open review](steering/PRE_EXPORT_REVIEW.md) owns the concrete rework and
required tests. A green unit suite, successful cleanup, or an earlier review
tag cannot replace those missing boundary tests.

## Lessons retained

- Fresh evidence and exact identity matter more than a reassuring status label.
- Native healing is the first recovery mechanism when it has a valid source.
- Preserve ambiguity; a larger or newer file is not necessarily the desired file.
- Keep planner reasoning separate from mechanical execution.
- Verify possible writes before retrying.
- Treat backup recoverability as a tested lifecycle, not a successful copy exit.
- Keep proof provenance, uncertainty, and failed approaches visible.
- Publish portable conclusions; retain personal evidence privately.
