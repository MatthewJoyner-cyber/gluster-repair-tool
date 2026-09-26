# Project TODO

Use [release preparation](../docs/RELEASE_PREPARATION.md) for the final pre-push
sequence. Repository names are selected; creation and publication follow acceptance.

- [x] Implement R1 saved execution-origin binding and refusal tests in the
  candidate (2026-09-20). See [binding scope and limits](../docs/EXECUTION_BINDING.md).
- [x] Add R1 volume UUID and precise backend freshness checks (2026-09-24).
  Installed CLI refusal passed for same-name/same-endpoint volume recreation
  and changed file data with preserved size/mtime; unchanged metadata passed.
  Review remains part of final remediation acceptance; the check is not a lock.
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
- [x] Qualify R6 on scoped disposable Ubuntu 24.04 guests (2026-09-24): real
  SSH/deployed helpers, trusted/user xattrs, numeric ownership, ACLs, timestamps,
  symlinks and cross-artifact hardlinks. Retained-archive recovery and a new
  archive/verified-cleanup/restore cycle passed independent comparisons on two
  remote guests after fixing read-back verification. Other platforms and original
  repair-step backup fidelity remain outside this proof.
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
  accounting, and a local diagnostic bundle for optional reports to the tool
  maintainers (2026-09-20; synthetic tests only). The initial redaction approach
  is superseded by the metadata exporter below. Private output modes and
  explicit file-content refusal remain. See [evidence limits](../docs/SUPPORT_EVIDENCE.md).
- [x] Replace diagnostic raw-text copying with versioned metadata projections
  and test real manifest/observation/plan/apply/execution/status writers plus
  AFR inspector output (2026-09-20; local synthetic tests). Names and paths
  become aliases; unknown fields/formats are omitted. Human review remains
  required. See [supported formats and limits](../docs/SUPPORT_EVIDENCE.md).
- [ ] Qualify diagnostic format coverage against intended operator collection
  workflows. Parsed volume-info/status and version-1 health reports export
  selected topology and readiness fields; unknown log and worker-wrapper labels
  are refused before reading, and recognized labels without a schema are
  inventoried as omitted. Ten saved workflow artifacts, an arbiter-volume
  description, one offline brick status and read-only AFR inspection on all
  three bricks exported as metadata with identifier scans passing. Gluster 11.1
  omitted the disconnected status field in `heal_info`; qualify that format on
  a version that emits it. Add only explicitly bounded schemas; do not
  reintroduce raw-copy fallback. Follow the
  [diagnostic collection checklist](../docs/DIAGNOSTIC_COLLECTION_QUALIFICATION.md).
- [x] Implement R12 evidence-build/repair-meta log forwarding on all five
  focused routes and record the path in status/report (2026-09-20; synthetic
  dispatch, real local log creation, and write-failure tests).
- [x] Qualify R13 local portability in a clean file-only candidate: fresh
  unprivileged HOME/XDG state, explicit overrides, missing/unwritable roots,
  installed help and the full offline suite (2026-09-20, Linux/Python 3.12.3).
  Fix the test expectation that froze an import-time default across an XDG
  environment change. See [portability limits](../docs/PORTABILITY.md).
- [ ] Finish R13 qualification beyond the tested Ubuntu 24.04 guests: supported
  interpreter/distribution matrix and physical-host policy. Current live
  evidence is limited to the three tested Ubuntu 24.04 guests. Follow the
  [fresh-account test sequence](../docs/BOOTSTRAP.md):
  separate initial administrator access from automated bootstrap, use three
  disposable OS instances, and test real service login and every peer pair.
  `sudo -n true` alone does not prove permission to run the installer.
  The three-VM Ubuntu 24.04 baseline, installation, service restrictions, six
  directed peer logins and idempotent reinstall passed on 2026-09-24. The
  changed-host, locked-key, transfer, package-validation and sudoers-validation
  fault cases all refused safely; the distribution-matrix and physical-host
  limitation remains. The SCP failure left installed files, sudoers and the
  service key unchanged and cleaned remote staging. After the mechanical
  repair, all three sequential reboots recovered peer links and zero-entry heal
  status before the next reboot. One independent missing-replica repair passed;
  the latter used an empty synthetic file.
- [x] Fix deploy-script preview/preflight side effects locally (2026-09-20):
  health refresh only after copying, no implicit key generation, read-only
  remote probes, verified host keys and early custom-layout refusal. Nine
  stubbed-command tests cover refusal and no-write behavior. See
  [deploy preview limits](../docs/DEPLOY_PREVIEW.md).
- [x] Audit the then-153-file candidate and generated local install tree
  (2026-09-20): inventory, links, syntax, source notices, byte-identical
  installed files, README help/test commands, privacy scan, and the candidate's
  local Git identity/history. See [validation](../docs/VALIDATION.md).
- [x] Re-audit the current 157-file staged inventory (2026-09-24): exact file
  list, source notices, file-only copy with fresh HOME/XDG and no PYTHONPATH,
  local-link checks, documented source help, the full offline suite, and a
  byte-identical 68-file local installed tree. The external private-identifier
  scan reported zero findings without echoing patterns. See
  [validation](../docs/VALIDATION.md). No host or Gluster service was contacted.
- [x] Repeat the inventory/privacy/link/file-only/installed-tree audit after
  freezing the final source list (2026-09-26): exact 161-file copy and hashes,
  119 local links, notices, fresh HOME/XDG, cleared inherited overrides, 845
  tests (one ACL skip), zero source/installed privacy findings and 69
  byte-identical installed files. See [validation](../docs/VALIDATION.md).
  This does not replace clean-host, platform-matrix or live acceptance.
- [x] Establish initial public provenance (2026-09-20): owner declaration,
  source/dependency inventory and future-contribution rule are recorded in
  [PROVENANCE.md](../PROVENANCE.md). Review each later external contribution
  before merge.
- [ ] Review all remediation, then create this repository's first completed
  review tag. Publish only after acceptance; keep the predecessor private.

Personal incidents and lab follow-ups belong in the private ledger. The agent
companion has its own release queue and does not unblock this one.
