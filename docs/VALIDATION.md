# Migration candidate validation

Schema-backed maintainer diagnostic export checked 2026-09-20 on Linux with
CPython 3.12.3:

- A 155-file clean copy passed 832 offline tests under UID 1000 with fresh
  HOME/XDG state; one ACL-dependent test was skipped.
- Real manifest, observation, plan, apply, execution and status writers,
  plus the AFR inspector with stubbed xattr reads, generate synthetic fixtures.
  Tests verify consistent host/path/action/ownership aliases while preserving
  GFIDs, sizes, modes, roles, defined action outcomes and AFR counters.
- Unknown keys, nested free text, messages, command previews, wrong types and
  unsupported formats/versions are excluded. Explicit file-content fields,
  binary/NUL input, symlinks, FIFOs, directories and oversized files are refused
  before output creation. Private modes and missing/omitted inventory entries
  are checked. No live host or user incident was used.
- The exact inventory, 113 Python parses, 86 local Markdown links, external
  private-identifier scan and whitespace checks pass. All 123 Python/shell
  sources have license notices. The local installer produces 68 files identical
  to their sources; six installed/source entry-point help commands pass.
- Diagnostic export now has no raw-copy fallback. Coverage qualification for
  additional collector formats remains open; unsupported inputs are explicitly
  omitted. Human review is required before sharing. See
  [supported formats and limits](SUPPORT_EVIDENCE.md).

Earlier staged-candidate audit checked 2026-09-20 on Linux with CPython 3.12.3:

- The 153-file inventory exactly matches the candidate tree, with no listed
  symlinks. All 111 Python sources parse and 84 local Markdown links resolve.
- A file-only copy under UID 1000 with fresh HOME/XDG state passed the README's
  `python3 -m unittest discover -s tests -v` command: 822 tests, one
  ACL-dependent skip. The three documented source help commands passed under
  the same isolated environment.
- The real local installer produced 67 files, each byte-identical to its source
  in the clean copy. Manager, core CLI and worker help succeeded from the
  installed tree outside the checkout. This is local packaging proof only.
- All 121 Python/shell source and test files have a GPL-2.0-only SPDX notice;
  ten missing notices were added. COPYING contains the GPL v2 text. A source
  search found no embedded third-party URL, copyright or adaptation header.
  This is an inventory check, not a determination of authorship or rights for
  any external contribution. The companion's license remains a separate gate.
- The external private-identifier scan found zero matches. Local Git history
  has two commits, both using the owner-selected public name and address.
  Archived trees and messages for both commits also passed that scan. No
  predecessor history, remote or tag is present. No host was contacted.

Deploy preview/preflight remediation checked 2026-09-20 on Linux with CPython
3.12.3:

- A 153-file clean candidate copy passed 822 offline tests under UID 1000 with
  fresh HOME/XDG state; one ACL-dependent test was skipped.
- Nine focused tests first reproduced health/cache work in preview, implicit
  key generation, remote mkdir/rm probes, `accept-new` host checking and late
  layout refusal. They now cover dry-run and preflight no-write behavior,
  partial-key refusal and health refresh only after a stubbed successful copy.
- All 111 Python sources parse; 83 local Markdown links resolve. The external
  identifier privacy scan, ten shell syntax checks and diff whitespace check
  pass. The [deploy preview contract](DEPLOY_PREVIEW.md) distinguishes these
  local checks from deployed-host qualification.
- No brick host or live Gluster volume was contacted. Actual SSH/sudoers,
  install permissions, rsync and health behavior remain unqualified.

R13 local portability checked 2026-09-20 on Linux with CPython 3.12.3:

- A file-only copy from the 151-file public inventory, with no Git database or
  private files, passed the full suite under UID 1000, a fresh HOME/XDG state
  and isolated Python environment: 813 tests, one ACL-dependent skip.
- Four process-level tests cover installed Python entry-point help from outside
  the checkout, fresh user-state creation, explicit work/backup overrides,
  unusable roots and permission-denied roots. Two older tests had frozen the
  import-time default across a changed XDG environment; they now assert the
  active process default.
- All 110 Python sources parse; 78 local Markdown links resolve. The external
  identifier privacy scan, ten shell syntax checks and diff whitespace check
  pass. See [tested scope and deployment limits](PORTABILITY.md).
- Only this local Linux/Python pair was available. Separate clean-host account,
  ownership, sudoers, Gluster and broader interpreter/distribution qualification
  remain open; no live volume was contacted.

R12 implementation checked 2026-09-20 on Linux with Python 3.12.3:

- Complete suite in a disposable HOME with inherited GLUSTER/XDG settings and
  PYTHONPATH removed: 809 tests, passed with one ACL-dependent skip.
- Both public manager commands forward `--log-out` through each of the five
  focused evidence routes. A real local path-route run creates the log and
  records it in status; a simulated permission denial stops before manifest
  and status output. Status reporting exposes the recorded path.
- The candidate inventory has 149 files; all 109 Python sources parse and 71
  local Markdown links resolve. The external-identifier privacy scan, all ten
  shell syntax checks and diff whitespace check pass.
- No live host or Gluster evidence was contacted for this validation. R13
  clean-host and platform qualification remains open.

R11 implementation checked 2026-09-20 on Linux with Python 3.12.3:

- Complete suite in a disposable HOME with inherited GLUSTER/XDG settings and
  PYTHONPATH removed: 805 tests, passed with one ACL-dependent skip.
- Eight focused regressions cover path/GFID/index live split-brain identity,
  path-only unknown identity, synthetic `localhost` source accounting, an
  unavailable live guard, read-only AFR inspection, copied-bundle redaction,
  missing artifact inventory and refusal of an unlisted private contact.
- The candidate inventory has 148 files; all 108 Python sources parse and 71
  local Markdown links resolve. The external-identifier privacy scan, all ten
  shell syntax checks and diff whitespace check pass.
- No live host, AFR output, prior resolver record or support submission was
  collected in this local pass. See [support evidence and remaining work](SUPPORT_EVIDENCE.md).

R10 implementation checked 2026-09-20 on Linux with Python 3.12.3:

- Complete suite in a disposable HOME with inherited GLUSTER/XDG settings and
  PYTHONPATH removed: 797 tests, passed with one ACL-dependent skip.
- Seven new matcher tests cover exact volume-relative paths, neighbouring
  basenames/prefixes, similar mount roots, trailing slashes, unparsed text,
  split-brain suffixes and canonical/nonmatching GFID rows. The focused
  canary suite passed 152 tests. One older fixture was updated from an
  unparsed mount path to a Gluster-style row.
- The matcher is used at both file metadata canary capture sites. It does not
  turn a saved observation into current repair evidence. Live Gluster output
  and R1 identity qualification remain open. See [matching limits](HEAL_ROW_MATCHING.md).
- All 106 Python sources parse; file inventory, local documentation links,
  privacy scan, shell syntax and diff whitespace checks pass.

R9 implementation checked 2026-09-20 on Linux with Python 3.12.3:

- Complete suite in a disposable HOME with inherited GLUSTER/XDG settings and
  PYTHONPATH removed: 790 tests, passed with one ACL-dependent skip.
- Ten new eligibility tests cover valid x3 data/arbiter fixtures, legacy or
  conflicting x4 topology, unsupported versions/provenance, pending or partial
  state, missing/wrong split-brain rows, canonical GFID rows, and mismatched
  identities/metadata. Creator coverage also refuses an arbiter source before
  volume mutation. The pre-fix run admitted 56 invalid state variants.
- All 105 Python sources parse; the 143-file inventory and local documentation
  links match; the external-identifier privacy scan is clean. The modified
  shell helper passes syntax checking, as does the diff whitespace check.
- This is a saved canary-state bridge, not live proof of a current split-brain
  row. Exact matching at capture time remains R10, and R1 live identity plus
  privileged/deployed qualification remain open. See [bridge limits](CANARY_SOURCE_CHOICE.md).

R8 implementation checked 2026-09-20 on Linux with Python 3.12.3:

- Complete suite in a disposable HOME with inherited GLUSTER/XDG settings and
  PYTHONPATH removed: 779 tests, passed with one ACL-dependent skip.
- Eleven helper-contract tests run generated commands through the actual
  shell helper, with SSH/sudo transport and low-level operations stubbed.
  The pre-fix run reproduced mdata/mkdir/stat incompatibilities, malformed
  xattrs reaching setfattr, and a failed ACL read masked by pipeline success.
- Tests cover mdata preview/executor agreement, GFID/AFR values and invalid
  encodings, metadata commands, quote-preserving operands, GFID links,
  planned backup/revert argv, rsync dispatch and failure propagation.
- All 103 Python sources parse; shell syntax, local documentation links and
  the file inventory pass. The external-identifier privacy scan is clean.
- Deployed helper/privilege qualification and Gluster-specific metadata
  semantics remain open. These local checks do not authorize live writes or
  close the remaining R9-R13 findings. See [helper limits](HOST_HELPER_CONTRACT.md).

R7 implementation checked 2026-09-20 on Linux with Python 3.12.3:

- Complete suite in a disposable HOME, with inherited GLUSTER/XDG settings and
  PYTHONPATH removed: 768 tests, passed with one ACL-dependent skip.
- Twelve bootstrap tests cover the real installer in a fresh directory outside
  the checkout, all six entry-point help commands, repeat upgrades, missing or
  invalid packages, package-preserving simulated transport, sudoers/helper argv
  parity, unsupported layouts, and preflight success/failure without key
  generation or staging. Volume preflight also requires a key for every peer
  and avoids temporary files. Missing operator public keys and partial service
  keypairs fail before transport.
- All 102 Python sources parse and all ten shell sources pass syntax checks.
  The candidate inventory and local documentation links match, and the
  external-identifier privacy scan is clean.
- Clean-host account creation, privileged file ownership, actual sudoers
  validation, the broader interpreter/platform matrix and live acceptance
  remain open. Separate deploy-script preview side effects are recorded in TODO.
  See [bootstrap contract and limits](BOOTSTRAP.md).
- R8-R13 and prior live/privileged qualification items remain open. R7 changes
  are not a completed-review checkpoint.

R5/R6 implementation checked 2026-09-20 on Linux with Python 3.12.3 and rsync 3.2.7:

- Complete suite in a disposable HOME, with inherited GLUSTER/XDG settings and
  PYTHONPATH removed: 756 tests, passed with one ACL-dependent skip.
- Nine journal tests cover intent before dispatch, command-level records,
  process exit, exception/interruption, parallel worker failure, journal/result
  and summary-write failures, immutable attempt IDs and CLI replay refusal.
- Nine backup-fidelity tests cover actual local archive -> verified cleanup ->
  restore behavior, including a real rsync client/server protocol over local
  pipes, two distinct synthetic hosts, hardlinks across artifacts, xattrs,
  modes, nanosecond timestamps, symlinks, changed/corrupt backups, verification
  differences and metadata permission failures. Encoded privileged metadata
  survives an archival fixture; this does not establish privileged acceptance.
- All 101 Python sources parse; the candidate inventory and local documentation
  links match. The external-identifier privacy scan is clean, with no generated
  bytecode caches present.
- The local R6 checkpoint uses the owner-confirmed public Git author identity.
  No original Git history is imported, and no remote, publication or
  completed-review tag is authorized by this
  development checkpoint.
- Privileged trusted-xattr, numeric ownership and ACL qualification, deployed
  storage/helper behavior, R1 live identity checks, and R7-R13 remain open.
  See [journal limits](EXECUTION_JOURNAL.md) and [backup fidelity limits](BACKUP_FIDELITY.md).

R4 implementation checked 2026-09-20 on Linux with Python 3.12.3:

- Complete suite in a disposable HOME, with inherited GLUSTER/XDG settings and
  PYTHONPATH removed: 738 tests, passed with one ACL-dependent skip.
- Eight new grouped tests cover resolver response loss, permission failure,
  timeout, signals, empty/unrecognized output, mismatched paths, conflicting
  stdout/stderr, recognized ties/success/already-resolved outcomes, operational
  errors in mutation steps, exact optional-removal ENOENT, and backup-copy
  failure after successful parent creation. The pre-fix run exposed unsafe
  continuation; serial/parallel executor tests now block fallback and dependents.
- Existing recipe tests were corrected to reject failed native resolution and
  disconnected-mount success. Response fixtures now include complete paths.
- All 96 Python sources parse; the inventory and local documentation links
  match. The external-identifier privacy scan found no matches, and no generated
  bytecode caches are present.
- Tests mock command/cluster boundaries and use disposable local artifacts.
  They do not qualify live Gluster versions, locales, or file contents. See
  [outcome contracts](EXECUTION_OUTCOMES.md). R5 durable per-command attempt
  records remain open. No Git repository was initialized or published.

R3 implementation checked 2026-09-20 on Linux with Python 3.12.3:

- Complete suite in a disposable HOME, with inherited GLUSTER/XDG settings and
  PYTHONPATH removed: 730 tests, passed with one ACL-dependent skip.
- Eleven new tests cover failed/skipped/unknown prerequisites, transitive and
  multiple dependencies, serial/parallel execution, stage-only work, invalid
  graphs, old outcomes, worker/scheduler interruptions, CLI failure reporting
  and preservation of unknown write history. They assert dependent steps never
  start and independent work can continue when authorized.
- Real executor orchestration runs against mocked step/cluster boundaries and
  disposable artifacts. R4 native-resolver fallback and R5 durable attempt
  records remain open; these checks are not live qualification.
- All 94 Python sources parse; the file inventory and local documentation links
  match the candidate. The external-identifier privacy scan is clean, and no
  generated bytecode caches are present.

R2 implementation checked 2026-09-20 on Linux with Python 3.12.3:

- Complete core suite in a disposable HOME, with inherited GLUSTER/XDG settings
  and PYTHONPATH removed: 719 tests, passed with one ACL-dependent skip.
- Five new integration tests parameterize refused gates, snapshot requirements,
  successful execution/verification, executor interruption, and failed heal
  restoration across both public entry points. Refusal asserts zero executor
  calls and zero heal-policy changes, including an artifact's saved stop decision
  and invalid-action rejection with managed heal enabled.
- Wrapper tests cover plain execution, newer flags, preview, help and process
  arguments. Existing origin-binding and heal-preservation tests now exercise
  the shared implementation. The old controller test also asserts no execution.
- All 93 Python sources parse, the file inventory matches, local documentation
  links resolve, and the external-identifier privacy scan is clean. The manager's
  apply-run help command succeeds. No generated bytecode caches are present.
- Operations use mocked cluster boundaries and disposable artifacts. Live
  qualification remains pending. R3 progress is recorded above; resolver and
  durable journal findings remain open.

R1 implementation checked 2026-09-20 on Linux with Python 3.12.3:

- Complete core suite in a disposable HOME with inherited GLUSTER/XDG settings
  and PYTHONPATH removed: 713 tests, passed with one ACL-dependent skip.
  All 92 Python sources parsed successfully; the file inventory matches the
  candidate and contains no generated bytecode caches. The external-identifier
  privacy scan found no matches.
- Twelve new binding tests cover cross-volume status, replaced endpoints/roles,
  altered saved identities, legacy artifacts, bare observation caches, real
  build writers, continuations, filtering, and changes during health refresh.
  Refusal tests exercise the package CLI, plain manager and delegated managed
  heal route, with and without health checks. They assert no executor or heal
  mutation is reached on a failed binding.
- The prior health/controller gate fixtures now include valid origin bindings
  so those tests still reach their intended gates. Return codes alone do not
  prove that a refused run made no writes; R2 adds the missing boundary assertions.
- These checks use mocked cluster operations and temporary local artifacts.
  See [binding limits](EXECUTION_BINDING.md); no live repair acceptance is implied.

Checked 2026-09-19 on Linux with Python 3.12.3.

- Core suite in a disposable HOME with inherited GLUSTER/XDG settings and
  PYTHONPATH removed: 701 tests, passed with one ACL-dependent test skipped.
  The temporary filesystem rejected test ACLs; that behavior is not qualified.
- Fresh-home help checks passed for the manager, core CLI, worker,
  evidence-build, repair-meta, repair, and backup-restore entry points.
- All nine shell entry points passed bash -n.
- Privacy scan with an external private-identifier list passed. This includes
  comments, tests, examples, guides, and the summarized history.
- Four incident-linked fixture identifiers, personal host/user labels and
  addresses, and the personal sample dataset were replaced in the candidate.
- Two content-split tests lacked a mount-setup mock. Fresh-home testing exposed
  the defect; those tests now isolate mount setup.

The normal inherited-environment run also passed 701 tests before the fresh-home
check. Portable defaults preserve explicit local work/backup overrides.
No active runs, backups, mounts, service state, or remote hosts were migrated.

At this 2026-09-19 staging snapshot, no Git history was copied or initialized.
The predecessor's source and dirty review work were preserved in a private
checksum snapshot and remain unchanged. The candidate's later local commits
are recorded above; future details belong in commits, not a copied ledger.

Still open: the core review's safety and functionality findings, installed-host
bootstrap, end-to-end remote metadata fidelity, supported-platform matrix,
and final publication/attribution review. These checks are not live repair
acceptance and do not certify the candidate for unattended use.
