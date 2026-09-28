# Gluster Repair Tool

GlusterFS diagnosis, reviewable repair plans and guided recovery when normal
healing leaves an object unresolved. The tool collects brick and heal metadata,
then explains the evidence and proposed next step. Start with Gluster's native
healing; this tool does not replace it.

## Capabilities and limits

The tool links heal entries, paths and GFIDs to logical objects, builds
reviewable repair plans, and offers guided decisions, backups and verification.
Its main use case is **replica-3**: three data bricks, or two data bricks and an
arbiter. It also accepts other volumes that Gluster reports as `Type: Replicate`,
though those layouts have less live repair proof. An arbiter can help establish
identity but is never a source of file contents. The bootstrap and repair paths
reject other reported volume types, including `Distributed-Replicate` and
dispersed volumes.

This beta has one qualified repair-write path: a supervised nonempty
missing-replica file restore on an Ubuntu 24.04 / Gluster 11.1 replica-3 lab.
Other repair writes remain experimental; tool-driven full namespace heal and
native per-file resolver writes are disabled. See [beta proof](#beta-scope) and
the [compatibility profile](docs/GLUSTER_COMPATIBILITY.md) before a write.
Depending on demand, future work may add `Distributed-Replicate` support, then
consider dispersed or erasure-coded volumes and help with geo-replication
recovery. Those are not supported today.

## Quick start

This path assumes an existing supported replica volume and an administrator on
a Gluster node. Replace `example-volume` and `operator` with your volume and SSH
login. The tested baseline is Ubuntu 24.04 LTS with Gluster 11.1.

1. Finish normal Gluster healing and confirm you have a backup. On the
   controller, set up an SSH key and verified host keys for every brick host.
   The administrator login needs noninteractive sudo on those hosts; the
   controller also needs access to `gluster volume info`. Follow the
   [first-time setup steps](docs/BOOTSTRAP.md) if any of this is missing.
2. From a checkout on that controller, check access, then install the tool and
   its restricted `gluster-repair` service account on the volume's brick hosts:

   ```bash
   ./gluster-bootstrap-volume.sh -v example-volume -l operator --preflight
   ./gluster-bootstrap-volume.sh -v example-volume -l operator
   ```

3. Use the **simple mode** for your first investigation. Preview gathers
   evidence and proposes actions without repair writes:

   ```bash
   python3 gluster-manager.py repair --volume example-volume --preview
   ```

4. If a problem remains after native healing, review the preview, backups and
   [beta limits](#beta-scope), then start the guided session in a terminal:

   ```bash
   python3 gluster-manager.py repair --volume example-volume --interactive
   ```

The guided session explains decisions and asks before eligible writes. Plain
`repair --volume example-volume` also starts this simple flow when attached to
a terminal; `--interactive` makes the choice explicit. A preview may contact
hosts and mount paths, which can trigger Gluster healing, even though it does
not execute repair writes. Run these commands only on a cluster you administer.
For a separate health report, use
`python3 gluster-manager.py health-check --volume example-volume`.

## Beta scope

The public repair-write claim is one supervised **nonempty missing-replica
file restore** on a disposable Ubuntu 24.04 / Gluster 11.1 replica-3 lab. It
preserved content, GFID, ownership, mode and a user xattr after a staging
defect was fixed. Later operator-seeded ghost-handle and directory canaries
completed, but they do not qualify every repair recipe. Other writes remain
experimental; tool-driven full namespace heal and native per-file resolver
writes are disabled. See [validation](docs/VALIDATION.md), the
[compatibility profile](docs/GLUSTER_COMPATIBILITY.md), and
[safety rules](steering/SAFETY_INVARIANTS.md) before a write.

Development began on Gluster 10.x. The tool targets Gluster 10 and newer,
subject to per-feature gates; it makes no claim for earlier versions. Other
Linux distributions and Gluster 11.2 have not been qualified. This is version
`0.1.0` beta, licensed [GPL-2.0-only](COPYING); see
[maintainer credit](MAINTAINERS.md) and the [beta.3 release notes](docs/RELEASE_NOTES_v0.1.0-beta.3.md).

## Setup and further use

The [bootstrap guide](docs/BOOTSTRAP.md) covers checkout, administrator SSH
keys, host trust, passwordless sudo, preflight, installation and verification.
It supports only the `gluster-repair` service account at
`/var/lib/gluster-repair` and installed files under `/opt/gluster-repair`.
The separate [update script](docs/DEPLOY_PREVIEW.md) has its own preview and
preflight contract. Keep controller and deployed helper versions matched; see
the [host-helper contract](docs/HOST_HELPER_CONTRACT.md).

The core works without Codex or private helpers. The optional
`gluster-repair-agent` companion can guide setup and triage, but does not
install the core or grant access. For Gluster recovery context, read the
[operator guide](docs/GLUSTER_GUIDE.md).

The command-line interface also supports manifests, apply plans, bounded
directory comparisons and canaries. These are implemented interfaces, not
claims that every branch has live repair proof. See
`python3 gluster-manager.py --help` and the
[first-beta plan](docs/FIRST_BETA_RELEASE_PLAN.md).

For local inspection without a cluster, use Linux and Python 3.12 or newer:

```bash
python3 gluster-manager.py --help
python3 -m unittest discover -s tests -v
```

The broader interpreter and distribution matrix remains unqualified. See
[portability limits](docs/PORTABILITY.md) for required live tools and state
paths. The [implementation history](HISTORY.md) summarizes earlier design
discoveries without importing private records.

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
bounded beta scope; the release tag identifies the later documentation-frozen
candidate.
Do not copy the
private archive, personal configuration, runtime artifacts, or ledgers.
