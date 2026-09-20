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
- [ ] Next: R7 fresh bootstrap, then R8 host-helper contract gaps, with meaningful
  failure tests.
- [ ] Resolve R9-R12 legacy canary eligibility, exact heal matching, consistent
  evidence classification, and evidence-build logging.
- [ ] Finish R13 portability qualification. User-state defaults and explicit
  overrides are implemented in this candidate; fresh installation, interpreter
  compatibility, and live acceptance remain open.
- [ ] Validate the exact clean tree, documentation commands, generated packages,
  privacy scan, licensing/attribution, and initial Git identity/history.
- [ ] Review all remediation, then create this repository's first completed
  review tag. Publish only after acceptance; keep the predecessor private.

Personal incidents and lab follow-ups belong in the private ledger. The agent
companion has its own release queue and does not unblock this one.
