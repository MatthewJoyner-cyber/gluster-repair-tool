# Gluster Repair Tool

GlusterFS evidence collection, heal and split-brain diagnosis, repair planning,
and guided recovery tooling. When normal Gluster healing leaves an object
unresolved, the tool gathers brick and heal metadata, builds a reviewable
plan, and guides an operator through the supported recovery path. It does not
replace native healing.
Development began on Gluster 10.x. The tool targets Gluster 10 and newer,
subject to the [per-feature compatibility checks](docs/GLUSTER_COMPATIBILITY.md);
versions before 10 have no compatibility claim.
Version: `0.1.0` beta. Copyright holder: see
[MAINTAINERS.md](MAINTAINERS.md). License: [GPL-2.0-only](COPYING).

**Beta scope is limited.** On a
disposable Ubuntu 24.04/Gluster 11.1 replica-3 lab, one supervised nonempty
missing-replica restore preserved content, GFID, ownership, mode and a user
xattr after a staging defect was fixed. Two further operator-seeded canaries
exercised ghost-handle cleanup and a two-stage directory repair on 11.1; see
[validation](docs/VALIDATION.md) for their limits. Use the current tree for
development, offline analysis and explicitly scoped testing. See the
[first-beta plan](docs/FIRST_BETA_RELEASE_PLAN.md) and
[open review](steering/PRE_EXPORT_REVIEW.md) before repair writes.

Start with [Gluster: recovery limits and this tool](docs/GLUSTER_GUIDE.md).
The [implementation history](HISTORY.md) summarizes the private
predecessor's design discoveries without importing its ledger. Future change
details belong in Git commits.

## Quick start

Start with a healthy, accessible cluster and try Gluster's native healing
first. On the tested Ubuntu 24.04 LTS / Gluster 11.1 combination, install the
tool and its restricted service account as described in the
[bootstrap guide](docs/BOOTSTRAP.md). From a source checkout, the first
tool commands are:

```bash
python3 gluster-manager.py --help
python3 gluster-manager.py health-check --volume example-volume
python3 gluster-manager.py repair --volume example-volume --preview
```

Replace `example-volume` with your volume name. The health check queries the
cluster; a repair preview may collect remote evidence and trigger mount
lookups, so run it only against a cluster you administer. Review the saved
evidence and proposed actions before using the guided interactive flow:

```bash
python3 gluster-manager.py repair --volume example-volume --interactive
```

The interactive flow asks before ready-safe writes. The beta's public
repair-write scope remains a supervised nonempty missing-replica restore
on the stated lab baseline. Other write recipes remain experimental despite
the representative lab canaries above; full
namespace healing and native-resolver tool writes are disabled. See the
[compatibility profile](docs/GLUSTER_COMPATIBILITY.md) and
[safety rules](steering/SAFETY_INVARIANTS.md) before executing any repair.

## Capabilities

- Manager/worker discovery of logical objects from heal rows, paths, GFIDs,
  GFID-child entries, or backend evidence.
- Manifest, plan, apply-preview, decision cards, and bounded directory comparisons.
- Guided repair, explicit execution, health/heal control, and verification,
  within the qualification limits above.
- Backup/restore and canary commands, with the limitations in the open review.

These are implemented interfaces, not guarantees that every branch is qualified.
The core has no dependency on Codex, installed agent skills, or private helpers.
An optional agent companion is maintained in a separate tree.

## Start from source

Use Linux and Python 3.12 or newer for this candidate. The broader interpreter
and distribution matrix remains unqualified. Source-checkout help and unit
tests do not require a live Gluster cluster:

```bash
python3 gluster-manager.py --help
python3 gluster-heal-tool.py --help
python3 gluster-worker.py --help
python3 -m unittest discover -s tests -v
```

See [local portability and deployment limits](docs/PORTABILITY.md) for the
tested environment, state paths and remaining clean-host checks. The
[Gluster compatibility profile](docs/GLUSTER_COMPATIBILITY.md) records which
write commands and output shapes have live qualification.

Live operations additionally need the matching Gluster client/CLI, SSH and
authorized remote workers. Individual operations use rsync, tar, attr/ACL
utilities, mount tools, and constrained privilege on brick hosts. Inspect each
operation's help and host requirements before deployment.

Bootstrap preserves the Python package layout and checks all installed entry
points. Local tests cover fresh installation, repeated upgrades and preflight
without key creation or staging. Only the default service account and install
paths are supported. Fresh installation, service restrictions, peer SSH,
repeat installation, selected failure cases and sequential reboots passed on
three disposable Ubuntu 24.04 guests; additional platforms remain open. See the
[bootstrap contract](docs/BOOTSTRAP.md) for commands and limits.
The separate update script's [dry-run and preflight contract](docs/DEPLOY_PREVIEW.md)
keeps previews read-only; its remote deployment still needs host qualification.
Controller and deployed helper versions must agree; see the
[host-helper contract](docs/HOST_HELPER_CONTRACT.md) for supported metadata
commands and the limits of the scoped privileged qualification.

## Evidence and planning

Use `python3 gluster-manager.py COMMAND --help` for arguments. The usual stages
are `manifest-build` or `evidence-build`, then `plan-build`, then
`apply-build` and review. `repair-meta` is an alias for focused evidence
collection. `repair --preview` previews proposed repairs, but collection can
contact hosts and mount lookups can trigger Gluster healing.
For `gluster-manager.py evidence-build` or `repair-meta`, `--log-out PATH`
writes resolver progress to that path. A successful run records it as
`evidence_log` in status and shows it in `status-report`; an unwritable log
destination fails the run before its manifest is written.

A saved artifact is not perpetual permission to execute it. New apply files
carry [volume/evidence binding](docs/EXECUTION_BINDING.md); legacy unbound files
must be rebuilt before execution. All public `apply-run` forms now share the
same execution gates and heal-restoration lifecycle. [Dependency checks](docs/EXECUTION_DEPENDENCIES.md)
block actions unless their prerequisites completed in the current run.
[Native resolver outcome checks](docs/EXECUTION_OUTCOMES.md) stop unknown results
before fallback; operational errors cannot count as harmless missing targets.
[Durable attempt records](docs/EXECUTION_JOURNAL.md) preserve command intent and
results through ordinary exceptions and process loss. Live qualification and
the other open findings still block unattended release use.
The optional [maintainer diagnostic bundle](docs/SUPPORT_EVIDENCE.md) lets a
user prepare pseudonymized metadata to share after review. It exports defined
fields, records unsupported formats as omitted, and replaces names and paths
with aliases. It does not read server file contents or transmit anything.

## Local storage

Controller work defaults to `$XDG_STATE_HOME/gluster-repair/work`, or
`$HOME/.local/state/gluster-repair/work` when XDG_STATE_HOME is unset or relative.
Set `GLUSTER_REPAIR_WORK_ROOT` explicitly to retain an existing work location.

Backup discovery defaults to the sibling `gluster-repair/backups` directory.
Override it with `GLUSTER_REPAIR_BACKUP_DIR` or `backup-restore --backup-dir`.
An explicit `--archive` or existing status-recorded archive takes precedence.
No old state, mount, or archive is moved automatically. Changing defaults does
not migrate or verify old archives. New [verified backup archives](docs/BACKUP_FIDELITY.md)
preserve selected metadata and hardlinks and require verification before cleanup.
Legacy archives cannot establish fidelity for automatic deletion of recovery copies.

Personal instructions and operational ledgers belong outside both public
repositories. See [the private/public boundary](docs/PRIVACY.md).

## Development and release

- [Release preparation](docs/RELEASE_PREPARATION.md): local readiness and
  publication gates. Repository destinations are in [MAINTAINERS.md](MAINTAINERS.md).
- [Current TODO](steering/TODO.md): unresolved implementation and release work.
- [Implementation history](HISTORY.md): frozen prehistory and discoveries.
- [Validation](docs/VALIDATION.md): checks for this migration candidate.
- [New Gluster major version](docs/MAJOR_VERSION_QUALIFICATION.md): focused
  compatibility qualification without replaying every historical canary.
- [Migration guide](MIGRATION.md): two independent repositories and private state.
- [Source provenance](PROVENANCE.md): public ownership and contribution record.
- [Safety invariants](steering/SAFETY_INVARIANTS.md): required repair contracts.
- [Architecture](docs/ARCHITECTURE.md): module ownership and tool boundaries.
- [Test plan](steering/TEST_PLAN.md): synthetic and live proof requirements.
- [Canary cases](docs/CANARY_CASES.md): public reports, tested families and proof limits.

The legacy canary CLI has built-in sample volume names. Always pass the
intended disposable `--volume` explicitly; no name proves a volume is safe.
The sample observations are wholly synthetic.
Saved POSIX canary source-choice plans use the
[versioned evidence bridge](docs/CANARY_SOURCE_CHOICE.md); older and direct
replica-4 fixture states remain diagnostic.
Canary heal visibility requires an [exact parsed row](docs/HEAL_ROW_MATCHING.md).

The private predecessor is a reference archive. Local development checkpoints
start from this sanitized source tree; no original Git database, tags, remotes
or author metadata are imported. The signed completed-review tag records the
bounded beta scope; publication requires the remaining release checks.
Do not copy the
private archive, personal configuration, runtime artifacts, or ledgers.
