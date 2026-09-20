# Project TODO

- [x] Implement R1 saved execution-origin binding and refusal tests in the
  candidate (2026-09-20). See [binding scope and limits](../docs/EXECUTION_BINDING.md).
- [ ] Review/qualify R1 on disposable live fixtures. Same-name volume recreation
  with identical endpoints and live object changes require additional identity
  checks; artifact fingerprints alone do not establish current file contents.
- [x] Implement R2: route every public apply-run form through the shared gates,
  validate before heal changes, preserve heal settings through refresh, and
  test refusal/interruption/restore failures (2026-09-20; local tests only).
- [x] Implement R3: require completed prerequisites from this execution, block
  transitive dependents, record interrupted/unknown outcomes, and report partial
  runs as incomplete (2026-09-20; serial/parallel local tests).
- [x] Implement R4: stop unknown native-resolver outcomes before fallback and
  reject operational failures as missing targets (2026-09-20; local fault tests).
  See [response contracts and limits](../docs/EXECUTION_OUTCOMES.md).
- [x] Implement R5 durable attempt records, unknown-attempt refusal and CLI
  failure handling (2026-09-20; local crash/fault tests). See
  [journal guarantees and limits](../docs/EXECUTION_JOURNAL.md).
- [x] Implement R6 metadata-preserving transfers, archive verification, guarded
  cleanup and verified restoration (2026-09-20; local rsync/filesystem tests).
  See [backup fidelity scope](../docs/BACKUP_FIDELITY.md).
- [ ] Qualify R6 on explicitly scoped disposable privileged fixtures: trusted
  xattrs, numeric ownership changes, ACLs and deployed remote-helper versions.
- [x] Record the requested local R6 checkpoint with the confirmed public author
  identity. This is a development checkpoint, not release acceptance.
- [x] Implement R7 package-preserving bootstrap, installed entry-point checks,
  preflight without key generation/staging, and brick-only sudoers argument
  parity (2026-09-20; disposable local installation and transport tests).
  See [bootstrap contract and qualification limits](../docs/BOOTSTRAP.md).
- [x] Implement R8 host-helper mdata/encoding checks, mkdir/stat command parity,
  and ACL source-failure propagation (2026-09-20; generated-command tests with
  stubbed low-level operations, including backup/revert commands). See
  [helper contract and limits](../docs/HOST_HELPER_CONTRACT.md).
- [x] Implement R9 saved POSIX canary source-choice eligibility: versioned
  x3 topology, identity/provenance, completed crawl, matching split-brain rows,
  and arbiter-source refusal (2026-09-20; synthetic refusal and bridge tests).
  See [source-choice scope](../docs/CANARY_SOURCE_CHOICE.md).
- [x] Implement R10 exact parsed heal-row matching against the recorded
  mount-relative path or canonical GFID (2026-09-20; collision and identity
  tests plus canary capture regression). See [matching limits](../docs/HEAL_ROW_MATCHING.md).
- [x] Implement R11 focused-route live split-brain guards, synthetic-seed
  accounting, and a redacted local support-draft builder (2026-09-20; synthetic
  tests only). See [support evidence and limits](../docs/SUPPORT_EVIDENCE.md).
- [ ] Collect available private AFR and prior resolver records through bounded
  routes, inventory missing artifacts, review the copied bundle, and prepare a
  case draft if an actual support submission is wanted. No rerun to fill gaps.
- [x] Implement R12 evidence-build/repair-meta log forwarding on all five
  focused routes and record the path in status/report (2026-09-20; synthetic
  dispatch, real local log creation, and write-failure tests).
- [x] Qualify R13 local portability in a clean file-only candidate: fresh
  unprivileged HOME/XDG state, explicit overrides, missing/unwritable roots,
  installed help and the full offline suite (2026-09-20, Linux/Python 3.12.3).
  Fix the test expectation that froze an import-time default across an XDG
  environment change. See [portability limits](../docs/PORTABILITY.md).
- [ ] Finish R13 release qualification on a separate clean host: service
  account, ownership, sudoers, deployed helpers, supported interpreter and
  distribution matrix, and scoped live acceptance. The local test is not a
  claim of those outcomes.
- [ ] Next: fix the separate deploy-script preview side effects below.
- [ ] Fix separate deploy-script preview side effects: health/cache work runs
  before dry-run/preflight, missing-key handling can generate keys, preflight
  creates remote probe directories and can accept new host keys. Add refusal
  and no-write tests; align unsupported custom layout handling with bootstrap.
- [ ] Validate the exact clean tree, documentation commands, generated packages,
  privacy scan, licensing/attribution, and initial Git identity/history.
- [ ] Review all remediation, then create this repository's first completed
  review tag. Publish only after acceptance; keep the predecessor private.

Personal incidents and lab follow-ups belong in the private ledger. The agent
companion has its own release queue and does not unblock this one.
