# Gluster Repair Tool v0.1.0-beta.3

First public beta of the standalone Gluster Repair Tool. The source tree
includes a manager, remote worker, guided repair preview and execution,
metadata-only diagnostic export, backup/restore helpers, tests and operator
documentation. The optional `gluster-repair-agent` Codex companion is released
at the matching tag; the core does not require it.

Start with the [README](../README.md) and [bootstrap guide](BOOTSTRAP.md).
Run Gluster-native healing first, inspect current health and backups, then
review the tool's saved evidence and preview before any write. The diagnostic
bundle uses aliases and metadata; it does not read server file contents or
submit a report automatically.

The declared test baseline is Ubuntu 24.04 LTS, Python 3.12.3, Gluster 11.1,
XFS lab bricks and a replica-3 volume. One supervised nonempty
missing-replica file restore was verified for content, GFID, ownership, mode
and a user xattr. Further operator-seeded ghost-handle and directory canaries
completed on Gluster 11.1, with narrower proof limits. The final installed
168-file candidate passed read-only replica-3 and replica-2 health and
zero-action preview checks; the replica-2 smoke did not execute a repair.
See [validation](VALIDATION.md) for the distinct evidence and limits.

Other repair-write recipes are experimental. Tool-driven full namespace heal
and native per-file split-brain resolver writes remain disabled. Development
began on Gluster 10.x; releases before 10 have no compatibility claim, and a
recognized pre-10 version produces a nonblocking precheck warning. Gluster
11.2, other Linux distributions and broad platform coverage are untested.
The [compatibility profile](GLUSTER_COMPATIBILITY.md) states the current
per-feature gates. This is best-effort open-source software without warranty;
do not treat a ready preview as proof that every repair path was live-tested.

Feedback on installation, unclear plans, changed Gluster output and remaining
repair cases is welcome through this repository's GitHub issues. Include the
tool and Gluster versions, OS/package source, topology class, expected and
observed behavior, and a **reviewed** optional metadata bundle. Replace
organization, host, path and network identifiers; do not attach credentials,
private topology or server file contents. See [support evidence](SUPPORT_EVIDENCE.md).

The CLI reports base version `0.1.0`; `beta.3` identifies this Git candidate.
The signed `review-2026-09-27` tag marks the completed scoped review before
the final release-documentation commits. This release does not expand that
review's repair-write scope.
