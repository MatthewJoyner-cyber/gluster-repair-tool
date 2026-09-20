# Pre-export Review: 2026-09-19

## Review checkpoint and outcome

This review covers the private predecessor before the source-only migration.
Source locations refer to that review snapshot; use the named function when
line numbers change. This is a source and test review, not fresh live acceptance.

**Do not move to the clean release repository yet.** Seven P1 findings and six
P2 findings remain. Runtime code was not changed by this review. No old review tag is imported into the clean history. Create its first review
checkpoint only after the scoped remediation is implemented and validated.

Candidate progress, 2026-09-20: R1 saved-origin binding, R2 shared execution
gates, R3 dependency-outcome checks, R4 command-outcome handling, R5 durable
attempts, R6 backup fidelity safeguards, R7 bootstrap fixes, R8 helper contracts
and R9 canary eligibility plus R10 heal-row matching
are implemented with local regression coverage. R1's [scope and remaining qualification](../docs/EXECUTION_BINDING.md)
are explicit; this does not close the other findings or establish live acceptance.
The findings below describe the reviewed predecessor snapshot.

Validation: all **699 existing tests passed**, Python compilation passed, and
all nine tracked shell entry points passed `bash -n`. Additional local probes
reproduced the failure modes below using mocked execution and disposable files.
No live repair, source selection, backend mutation, or service restart ran.
A green suite does not cover the missing integration and fault tests listed here.

## P1: Execution and rollback blockers

### R1. Bind execution to the volume and evidence that produced the apply plan

Locations: `gluster_heal_tool/cli.py:1372`,
`gluster-manager.py:1300`, `gluster_heal_tool/apply_reporting.py:84`.

Both execution entry points take the health/heal volume from the mutable status
file. Serialized apply results do not carry a required immutable origin volume
and topology/evidence binding. There is no pre-execution comparison between
that origin and the current status. A probe with actions for volume A and
status for volume B checked B's health/heal and reached execution of A's host.
This can check or manage healing on the wrong volume while modifying another.

Rework: record origin volume, brick identities, and evidence/plan fingerprints
in the apply artifact; reject mismatched or missing required bindings before
health management or any write. Refresh facts without silently changing the
approved plan. Require explicit rebuild for legacy unbound artifacts.

Tests: A-plan/B-status; replaced brick topology; changed target identity;
missing legacy binding; correct same-volume execution. Assert zero execution
and zero heal-policy mutation for every failed binding.

Candidate implementation: evidence collection records volume/brick roles;
plan and apply artifacts retain that origin and source fingerprints. Both
execution routes reject changed/missing lineage and compare fresh topology,
including when health checking is skipped. Refusal occurs before health/heal
management and before execution. Legacy artifacts and bare observation caches
remain preview-only. Tests also cover mutation during health refresh,
continuations, filtered execution and both real build writers. Live acceptance
and same-name volume recreation identity checks remain pending.

### R2. Plain manager apply-run checks the controller block after execution

Locations: `gluster-manager.py:783`, `:1356`, `:1373`.

Only four newer flags route manager apply-run through the canonical CLI. Plain
`apply-run --execute --batch` uses the legacy implementation, where
`controller_cycle_gate_warnings` runs after `execute_apply_results`.
The isolated probe returned the expected refusal code 2, but execution had
already been called despite a saved rebuild-plan block.

Rework: route every public apply-run form through one guarded implementation,
or establish complete equivalent pre-write gates before retaining compatibility.
Keep snapshot, interruption, verification, and heal-restoration semantics aligned.

Tests: parameterize plain and newer-flag forms across blocked controller state,
snapshot requirements, failed health, interruption, and normal execution.
Assert the executor is never called on a refused run.

Candidate implementation, 2026-09-20: every manager apply-run form delegates to
the package CLI; the duplicate executor branch and its unused helpers are
removed. Both status and artifact controller-stop decisions are enforced.
Action validation precedes heal-policy changes. The shared lifecycle preserves
the prior heal settings through post-execution refresh, records interrupted
execution as unknown, and blocks completion when restoration fails. Regression
tests cover plain, ready-only, managed-heal and snapshot forms, refused health
and authorization, normal execution/verification, interrupts and restore failure.
The complete candidate suite passes 719 tests with one ACL-dependent skip;
these mocked/local checks do not establish live acceptance. R3-R4 progress is
below, including the later R5-R6 implementation and qualification limits.

### R3. Keep-going ignores failed action dependencies

Location: `gluster_heal_tool/executor.py:873` and `:945`.

The executor orders waves but does not consult dependency outcomes before
starting an action. With keep-going enabled, a failed parent in wave 0 was
followed by its dependent child in wave 1, which was reported completed.
Checking that dependency IDs exist is not checking that they succeeded.

Rework: allow independent work to continue, but block the transitive dependents
of failed, unknown, or unmet prerequisite actions in both serial and parallel
execution. Preserve the reason in every blocked action.

Tests: parent failure with dependent child plus independent sibling; multiple
levels; skipped prerequisite; interrupted prerequisite; serial/parallel waves.
The child must never start and the independent sibling may continue.

Candidate implementation, 2026-09-20: dispatch requires every prerequisite to
have completed in an earlier wave of this run. Invalid dependency identities,
same/later-wave references and old action outcomes cannot authorize execution.
Failed, unknown, skipped and staging-only prerequisites block transitive
dependents. Keep-going permits independent work; handled interruption prevents
new waves and records stopped/blocked actions. Incomplete public runs return 2
before post-run healing or backup cleanup. Eleven new tests cover serial and
parallel dispatch, exception/interruption boundaries, multiple prerequisites,
bad graphs, saved outcomes, CLI reporting and unknown write history. Full suite:
730 tests passed with one ACL-dependent skip. See [dependency semantics and
limits](../docs/EXECUTION_DEPENDENCIES.md); this is not live acceptance.

### R4. Native resolver transport errors enable destructive fallback

Locations: `gluster_heal_tool/executor.py:211` and `:787`.

Except for the not-in-split-brain special case, every nonzero native resolver
exit becomes a nonblocking skipped step. The action then continues its planned
fallback. Injecting SSH exit 255 with connection loss caused the following
remove step to run and the action to finish with nonblocking skips. A resolver
may already have written before transport loss, so fallback authority is stale.

Rework: distinguish explicitly supported resolver outcomes from operational
failure. Stop on permission, transport, timeout, or otherwise unknown outcomes;
record possible writes, verify exact effects, and return to fresh planning.
Audit ENOTCONN/EIO/missing-message tolerance in other steps under the same rule.

Tests: resolver SSH loss after possible write, permission error, timeout,
recognized tie, already-resolved response, and successful resolution.
No destructive fallback may follow an operationally failed or unknown resolver.

Candidate implementation, 2026-09-20: a separate response classifier requires
recognized, path-matched native outcomes and compatible exit codes. Both output
streams participate; additional or conflicting diagnostics, transport/permission
errors, timeouts and unknown results stop the action before source-brick or
destructive fallback. Recognized selection ties permit only planned follow-up;
successful and already-resolved responses skip fallback. Missing tolerance is
restricted to an explicit optional removal's exact ENOENT diagnostic. Failed
backup-parent creation, copies, restore, staging, metadata writes and quarantine
cannot skip into later deletion. Eight new grouped tests reproduce the unsafe
cases and exercise real serial/parallel dispatch with mocked commands. Existing
tests that expected unsafe continuation were corrected. Full suite: 738 tests
passed with one ACL-dependent skip. See [response contracts and qualification
limits](../docs/EXECUTION_OUTCOMES.md). Live/version qualification remains open;
R5 progress is below. No automatic retry is authorized by these tests.

### R5. Exceptions can lose the execution record after a successful write

Locations: `gluster_heal_tool/executor.py:770`, `:910`, `:933`.

Per-action records are written after execution of the wave. An exception from
a step or future escapes before that persistence. In the probe the first step
returned success and the second raised OSError; the run directory contained
no files identifying the completed write. The canonical UI may record a broad
unknown state, but cannot reconstruct the absent command/result record.

Rework: persist an attempt record before each command and its outcome as soon
as known; handle exceptions and interruption without losing earlier results.
Preserve an unknown outcome for an in-flight write. Use distinct attempt IDs
so a later execution does not overwrite the previous resolver history.

Tests: success then OSError, KeyboardInterrupt, parallel future failure, report
write failure, restart during an in-flight step, and second execution attempt.
Verify prior results survive and resume cannot replay an unknown write.

Candidate implementation, 2026-09-20: immutable, fsynced attempt events now precede
step/command dispatch and immediately record known results. A run lock prevents
concurrent execution against one directory; interrupted, incomplete or unreadable
attempts block reuse. Saved-artifact/run association prevents a new run path from
bypassing an unresolved attempt through the CLI. Worker and report failures keep
earlier events; CLI completion and follow-up writes stop. Each attempt has a new
ID, preserving prior command history. Nine focused tests cover process exit,
exceptions, interruption, parallel work, write failures and replay refusal. See
[storage and retention limits](../docs/EXECUTION_JOURNAL.md). Local fault proof
does not establish deployed storage or remote-operation acceptance.

### R6. Remote backup staging loses metadata and hardlinks

Locations: `gluster_heal_tool/backup_maintenance.py:73`,
`gluster_heal_tool/remote_ops.py:85`, and
`gluster_heal_tool/backup_maintenance.py:906`.

Archive staging uses rsync -a without ACL/xattr/hardlink preservation. A local
transport-equivalent copy returned zero while dropping a user xattr and
splitting a hardlinked file pair. The archive can then be declared complete
and original remote backups cleaned. Restore adds -A/-X, but cannot recover
metadata already lost during staging; it also omits hardlink preservation.

Rework: define and implement end-to-end metadata fidelity for both transfers
and archive construction. Account for privileged trusted xattrs and numeric
ownership when staging as an ordinary controller user. Do not delete original
backups until required fidelity is verified; flags alone are insufficient proof.

Tests: actual filesystem archive -> cleanup -> restore -> usable rollback,
including ACLs, user/trusted xattrs, numeric ownership, hardlinks, symlinks,
multiple hosts, same pathname on different hosts, and permission failures.
Use disposable privileged fixtures for trusted-xattr acceptance; never infer it
from controller-local mocks.

Candidate implementation, 2026-09-20: version-3 archives capture a content and
metadata inventory. Host-grouped rsync transfers preserve cross-artifact hardlinks,
ACLs, xattrs and numeric IDs, using fake-super storage on the controller. Capture,
archive contents, current backups before cleanup, and restored destinations are
verified. Metadata errors, transport loss, changed sources, skipped conflicts and
verification differences retain recovery copies. Existing archives are replaced
only after successful capture/verification. Directory metadata and nanosecond
timestamps are restored, and legacy archives cannot authorize automatic deletion.
Nine focused tests include a real rsync client/server archive -> verified cleanup
-> restore cycle over local pipes, two distinct host roots, hardlinks across
artifacts, metadata and content checks, and injected failures. Encoded privileged
metadata is tested synthetically; privileged trusted-xattr, numeric ownership,
ACL and installed-host qualification remain open. See [contract and limits](../docs/BACKUP_FIDELITY.md).

### R7. Fresh bootstrap installs a flat, unimportable Python tree

Candidate implementation, 2026-09-20: transport and installation retain the
package directory. A shared installer validates sources before copying and
checks all six installed entry points outside the checkout. Host/volume
preflight does not create keys or staging files, brick-only sudoers matches
the helper argument order, and unsupported service accounts/homes/prefixes
fail explicitly. Local fresh/repeated-install and transport/failure tests pass.
Clean-host account/privilege/sudoers qualification remains open; the separate
deploy script's preview side effects are tracked in TODO. See the
[bootstrap contract](../docs/BOOTSTRAP.md).

Locations: `gluster-bootstrap-host.sh:272` and `:310`.

Package modules are copied into the same staging directory as entry scripts,
then copied directly into the install root. The required gluster_heal_tool
package directory is absent on a fresh host. Reproducing that exact layout in
a temporary directory made gluster-worker --help fail with ModuleNotFoundError.
A previously deployed package directory can mask the defect.

Rework: preserve the package directory during staging and installation. Validate
the installed entry points before declaring bootstrap complete. Audit the
related bootstrap contract: preflight currently calls service-key creation,
and brick-ops-only sudoers uses brick-down --brick while the helper requires
brick-down --volume ... --brick ....

Tests: isolated fresh install with no checkout/PYTHONPATH fallback; every installed
entry point --help; idempotent upgrade; preflight makes no key/filesystem changes;
brick-maintenance sudoers matches the exact helper argv. Test custom prefixes
and service-account options or reject unsupported combinations explicitly.

## P2: Functionality and evidence gaps

### R8. Directory mdata repair is rejected by its host helper

Candidate implementation, 2026-09-20: the helper accepts exact mdata writes,
checks GFID base64 and nonempty whole-byte mdata/AFR hex encodings, and rejects
extra operands. Generated-command tests also exposed and fixed missing mkdir
flags, the absent stat handler, and ACL source failures masked by pipeline
success. Tests run the actual helper with stubbed transport/low-level commands,
including planned backup/revert argv and injected failures. Privileged deployed
qualification remains open. See [helper contract](../docs/HOST_HELPER_CONTRACT.md).

Locations: `gluster_heal_tool/executor.py:323`, `gluster-host-ops.sh:67`.

The executor sends setfattr for trusted.glusterfs.mdata, but host-ops accepts
only trusted.gfid or trusted.afr.*. A direct helper invocation with a dummy
path failed at its allowlist before accessing the path. Existing tests check
the generated recipe, not its compatibility with the deployed helper.

Rework: align the bounded helper and planner/executor contract, including value
validation. Tests must run generated arguments through the actual helper with
stubbed low-level commands and cover every emitted operation, including reverts.

### R9. The replica-4 source-choice block does not cover old state

Candidate implementation, 2026-09-20: the bridge requires versioned state,
consistent x3 topology/metadata/roles, a data-brick source, completed crawl
flags, and exact matching rows in saved split-brain snapshots. It rejects
legacy/direct x4, pending-only, partial and conflicting states before plan
creation. The creator records roles and version and refuses an arbiter source
before mutation. Existing x3 bridge coverage now uses a supported fixture;
synthetic refusal tests cover legacy and misleading evidence. This is a saved
harness-state check, not fresh live proof. See [scope](../docs/CANARY_SOURCE_CHOICE.md).

Location: `gluster_heal_tool/canary_file.py:2324`.

The new guard checks only the new fixture_scope string. Legacy four-endpoint
fixtures lack that field and still pass. A four-data-brick state with empty
heal listings and the old true visibility boolean produced a source-choice
action. An existing test still explicitly expects a four-endpoint bridge to pass.

Rework: validate state version, topology, fixture provenance, and source-choice
eligibility; refuse legacy/unknown direct x4 state and retained pending-only
state until the required independent evidence exists. Preserve valid x3 use.

Tests: legacy x4 without scope, unknown scope, conflicting eligibility flags,
pending-only x3, missing/empty split-brain evidence, valid x3, and the explicit
new x4 diagnostic fixture. Do not rely on setup booleans as acceptance evidence.

### R10. Heal path matching accepts an unrelated suffix

Candidate implementation, 2026-09-20: both file metadata canary creation
paths compare parsed rows to the exact volume-relative mount path or a
canonical recorded GFID. Basename suffixes, neighbouring paths and unparsed
text no longer set visibility flags. Tests cover mount-root component
boundaries, trailing slashes, row suffixes, GFID identity and an existing
capture fixture updated to real parsed output. See [matching limits](../docs/HEAL_ROW_MATCHING.md).

Location: `gluster_heal_tool/canary_file.py:102`.

The new suffix match treats a heal row /payload.txt as evidence for
/mnt/canary/alpha/payload.txt. The older substring shortcut can also match a
longer neighbouring pathname. Thus an unrelated row can set visibility flags.

Rework: compare parsed entries to the exact volume-relative logical path using
the known mount root; support canonical GFID rows through an explicit identity
match. Do not infer the mount-relative path by arbitrary suffix matching.

Tests: same basename in different directories, pathname-prefix collisions,
mount roots containing similar path components, trailing slashes, exact logical
paths, and canonical/nonmatching GFID rows.

### R11. Recent support conclusions exceed the evidence

Local R11 remediation (2026-09-20): operator path/GFID/index/backend routes
now pass a fresh split-brain snapshot into manifests; unavailable identity
evidence blocks index cleanup, and `localhost` is excluded from brick proof.
The local support draft labels absent artifacts and the copied-bundle builder
redacts supplied identifiers. See [support evidence](../docs/SUPPORT_EVIDENCE.md).
Private collection, human redaction review, safe lab-control qualification,
and any actual upstream submission remain open; no live rerun is authorized by
this local change.

Locations: the predecessor's private support notes;
`gluster_heal_tool/worker.py:942`, `gluster_heal_tool/manager.py:881`,
`gluster_heal_tool/planner.py:1209`.

The worker already provides bounded inspect_afr_state for a canary-owned path;
host-ops getfattr being narrower does not establish that AFR collection is
unavailable. The reviewed GFID route can include a synthetic localhost source
without stale-index proof. Its review_dead_gfid_reference classification alone
does not prove a broken live GFID handle. The GFID route supplies localhost as
an operator seed and does not carry the live split-brain annotation used by the
mounted-path route. Do not simply delete the localhost guard: preserve actual
split-brain evidence before considering any cleanup.

Likewise, no cohort-isolation helper demonstrates a missing lab capability,
not that safe isolation is impossible or necessarily needs a shared-host
partition. Local drafts and raw artifact paths do not establish a sanitized
submitted support case. No upstream submission was performed in this session.

Rework: correct the support readiness/diagnostic claims; distinguish observed
facts, tool classification, and hypotheses. Reconcile GFID/index discovery
with fresh split-brain identity evidence. Collect available AFR and prior
resolver records through existing bounded routes, redact a copied bundle,
inventory missing artifacts, and prepare a submission draft. Reassess a safe
lab-control design before concluding impossibility. Preserve the no-rerun rule.

Tests: same object through path/GFID/index discovery retains the same live
split-brain guard; localhost is not treated as a real brick; read-only AFR
inspection invokes no mutation; bundle validation detects missing files and
private identifiers; no nonexistent artifact is labelled collected.

### R12. Evidence-build silently ignores --log-out

Local R12 remediation (2026-09-20): the public manager forwards `--log-out`
through all five focused routes for both `evidence-build` and `repair-meta`,
saves the path as `evidence_log`, and displays it in `status-report`. Synthetic
dispatch and real local file creation/failure tests pass. No live evidence
collection was part of this validation.

Location: `gluster-manager.py:868`.

The parser accepts --log-out, but none of the five evidence dispatch branches
passes log_path to its resolver. This explains the missing evidence log in the
recent handoff despite a successful command.

Rework: forward the option consistently and record the output path. Tests:
parameterized path/backend/GFID/GFID-child/index CLI dispatch and a temporary
log-file creation check, including unwritable-log failure.

### R13. Public runtime still defaults to private controller paths

Local R13 qualification (2026-09-20): the portable defaults and overrides
were exercised from a clean file-only copy under a fresh unprivileged HOME and
XDG state root, with no private mount or agent configuration. Offline tests,
installed entry-point help, missing/unwritable work-root refusal and status
path checks pass. One test had incorrectly compared an import-time constant
with a post-environment-change default; its expectation is now dynamic. See
[portability limits](../docs/PORTABILITY.md). Privileged clean-host deployment,
the broader interpreter/distribution matrix and live acceptance remain open.

Locations: `gluster_heal_tool/controller_paths.py:11`,
`gluster_heal_tool/backup_maintenance.py:26`.

At review time, default work and backup locations were tied to a private machine layout.
This candidate replaces those defaults with per-user state and explicit overrides;
full installation/platform qualification remains pending.
The keyword scan showing no agent dependency is useful, but does not establish
that a fresh unprivileged installation builds, tests, and operates independently.

Rework: choose documented portable defaults with explicit overrides, migrate
old state deliberately, and declare the supported Python/system dependencies.
Keep private paths and identifiers out of public examples and initial history.

Tests: fresh HOME/XDG environment, no private mounts, non-root startup, explicit
work-root override, missing/unwritable work locations, installed help commands,
and the supported interpreter/platform matrix. Finish clean-tree validation
before marking the self-contained-core release item complete.

## Rework order and acceptance

1. R1-R5: shared execution authority, entry-point parity, dependencies,
   operational failure handling, durable attempt records.
2. R6-R8: faithful backup lifecycle, fresh installation, helper contract parity.
3. R9-R12: fixture migration, exact evidence matching, corrected support
   collection/classification, working log output.
4. R13: portable runtime defaults and clean-install qualification.
5. Add each defect's failing regression before its fix. Run focused suites for
   each group, then the complete suite and shell/Python checks.
6. Validate risky runtime changes only on explicitly scoped disposable fixtures.
   Preserve unresolved local fixtures; consult private steering before live work.
7. Review the implemented fixes, record results and remaining live-proof limits,
   commit when requested, and create a distinct annotated review checkpoint.
   Do not import private predecessor tags.
8. Only then create and audit the clean release tree and its initial history.

Missing coverage is primarily at real boundaries: public CLI -> canonical
execution, saved plan -> fresh topology, prerequisite -> dependent action,
process failure -> durable report, planner argv -> installed shell helper,
remote backup -> archive -> rollback, and bootstrap -> importable installation.
The existing synthetic matrix, arbiter-role, rollback-path, and interaction
tests remain valuable; their passing results do not replace those boundaries.
