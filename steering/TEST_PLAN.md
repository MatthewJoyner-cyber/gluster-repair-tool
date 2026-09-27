# Test plan

The [first-beta plan](../docs/FIRST_BETA_RELEASE_PLAN.md) defines the finite
release checks and deferred work. Testing is best effort on the declared
Ubuntu LTS baseline, not an exhaustive Linux or Gluster certification.

From the repository root:

```bash
python3 -m unittest discover -s tests -v
```

Tests use synthetic identifiers and mocked remote calls. Some local backup
tests exercise filesystem behavior. Run help from a fresh source tree with no
private modules or agent helpers on the import path. Validate changed shell
scripts with bash -n.

Saved artifact binding has local regression coverage in `tests/test_apply_binding.py`
and `tests/test_execution_freshness.py`; installed same-name volume recreation
and same-size/mtime changed-data refusals passed in the scoped Ubuntu lab.
Public execution-gate parity and
interruption/restoration behavior are covered in `tests/test_apply_run_gates.py`.
Dependency outcomes and incomplete-run reporting are covered in
`tests/test_execution_dependencies.py`. Resolver response loss, permission errors,
recognized results, conflicting diagnostics and missing-target tolerance are
covered in `tests/test_executor_outcomes.py` with real executor dispatch and
mocked commands. Live resolver version/locale qualification remains open.
Durable attempts, process loss, journal/report failures and saved-artifact replay
are covered in `tests/test_execution_journal.py`. Archive fidelity and verified
cleanup/restoration have actual filesystem and local rsync-protocol coverage in
`tests/test_backup_fidelity.py`, including cross-artifact hardlinks and host
identity. Scoped privileged trusted-xattr, numeric-owner, ACL and hardlink
archive/restore checks passed on Ubuntu guests. Original repair-step rollback
remains a separate claim. Fresh installation and installer-failure checks also
passed there. Helper argv, legacy canary state, exact row matching, diagnostic
projection and logging have focused regression tests; see
[dated validation](../docs/VALIDATION.md) rather than the predecessor review's
historical remaining-work statements.

`tests/test_gluster_compat.py`, `tests/test_volume.py`,
`tests/test_heal_performance.py` and `tests/test_heal_parser.py` cover command
gates, unknown versions, missing/disconnected status, inconsistent counts and
one-shot/already-run behavior. The mocked successful full-heal and resolver
branches do not enable those features; current live qualification admits only
11.1 pending-index heal. Keep optional disabled commands out of beta blockers.

When live work resumes, follow item 1 of the first-beta plan: deploy matching
source/worker hashes, establish health, reuse native-heal-first results,
exercise one nonempty synthetic repair with independent verification, check
current preview/export behavior, restore settings and retain the powered-off
lab. If Gluster clears a canary, record the no-repair result; do not force a
residual fault merely to claim repair acceptance. Broader distro/recipe tests
are follow-up work. Run the full offline suite once against the frozen release
and repeat affected checks after further changes.

A live test must name a disposable target chosen by the operator, refresh
topology/heal/daemon state, retain independent evidence, and define cleanup.
An arbiter never supplies file payload. Do not rely on a volume name as a fence.
Never partition shared networking to satisfy a fixture precondition.

Proof labels distinguish unit/synthetic, harness-only, diagnostic-only,
native-heal-first, operator-path-mechanical, and independent repair acceptance.
A zero heal count after teardown does not itself prove the repair engine fixed
a fault. Keep exact live evidence and host mappings in the private ledger;
publish a reviewed abstract result or a deliberately synthetic example.
