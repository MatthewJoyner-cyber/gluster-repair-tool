# Test plan

From the repository root:

```bash
python3 -m unittest discover -s tests -v
```

Tests use synthetic identifiers and mocked remote calls. Some local backup
tests exercise filesystem behavior. Run help from a fresh source tree with no
private modules or agent helpers on the import path. Validate changed shell
scripts with bash -n.

Saved artifact binding has local regression coverage in `tests/test_apply_binding.py`;
live identity qualification remains open. Public execution-gate parity and
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
identity. Privileged trusted-xattr, numeric-owner and ACL qualification remains
open. The review still requires integration/fault coverage for fresh installation, real helper argv,
legacy canary state, exact path matching, GFID evidence, and logging.

A live test must name a disposable target chosen by the operator, refresh
topology/heal/daemon state, retain independent evidence, and define cleanup.
An arbiter never supplies file payload. Do not rely on a volume name as a fence.
Never partition shared networking to satisfy a fixture precondition.

Proof labels distinguish unit/synthetic, harness-only, diagnostic-only,
native-heal-first, operator-path-mechanical, and independent repair acceptance.
A zero heal count after teardown does not itself prove the repair engine fixed
a fault. Keep exact live evidence and host mappings in the private ledger;
publish a reviewed abstract result or a deliberately synthetic example.
