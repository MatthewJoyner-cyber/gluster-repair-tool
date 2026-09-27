# Migration candidate validation

## Final installed Gluster 11.1 read-only compatibility pass (2026-09-27)

The selected 168-file public source inventory was copied to the retained
Ubuntu 24.04 lab. All source hashes matched on the controller, normal bootstrap
and installation verification passed on three guests, and 70 installed runtime
hashes matched on each guest. This includes the later pre-10 health warning.
The validation and compatibility prose was updated after this staging run;
those documentation-only edits did not change the 70 installed runtime files.
On the existing healthy replica-3 volume, the installed health check was ready
(38/38 checks, three hosts and three bricks online); a fresh read-only preview
proposed zero actions. Heal info remained connected with zero entries on all
three bricks before and after.

A separate temporary two-brick replica was created solely for topology and
read-only compatibility. Gluster 11.1 reported `Type: Replicate` and `Number of
Bricks: 1 x 2 = 2`. Both bricks were connected and had zero heal entries. The
installed tool's health check was ready (27/27 checks, two hosts and bricks
online), and a healthy-file preview proposed zero actions. The fixture was
unmounted and its exact volume and bricks removed. Both retained three-brick
volumes then had three connected bricks and zero entries; all guests were
gracefully shut off and retained. No replica-2 repair write or new fault
builder was used.

The tool-facing version, volume type/brick layout, status, heal-info count and
connection checks were exercised by the installed 11.1 commands. Existing
focused parser, health, preflight, diagnostic-metadata and worker tests passed
against their historical and fail-closed fixtures. No changed 11.1 output shape
required a new fixture. The original Gluster 10 development work provides the
baseline assumptions, but no complete raw 10.x command transcript was retained
for a byte-for-byte output diff. This result qualifies the observed 11.1
read-only paths and preserves the earlier narrow replica-3 repair-cycle proof;
it does not establish a new Gluster 10 or replica-2 write claim.

## Gluster 11.1 failed-rename GFID-handle check (2026-09-27)

An existing disposable-lab probe exercised the
[failed-directory-rename handle fix](https://github.com/gluster/glusterfs/issues/2752)
listed in the [11.1 release notes](https://docs.gluster.org/en/main/release-notes/11.1/).
On a three-brick replica, renaming a directory over a nonempty destination
failed as expected. Independent checks found the source and destination GFIDs
and their backend handles unchanged and valid on every brick afterward. The
fixture was removed through the mount; both lab volumes returned to three
connected bricks and zero heal entries, and the VMs were shut down. Earlier
attempts had stopped before the rename because the lab probe used an incorrect
GFID handle-path format. This is a scoped native Gluster fix check, not a
repair-tool execution or proof of every rename/client variant.

## Reused repair-cycle canaries on Gluster 11.1 (2026-09-27)

Two existing canary builders were run on the retained Ubuntu 24.04, Gluster
11.1 replica-3 VMs. These were deliberately constructed backend faults with
healing held off, so their proof class is **operator-seeded live repair**, not
evidence that Gluster 11.1 naturally leaves these faults unhealed. The installed
candidate discovered each case from fresh brick/GFID or backend-path evidence,
without using the builder's state as planning input.

- **File-handle ghost:** One brick had a stale `.glusterfs` GFID handle whose
  backlink did not resolve. A GFID-seeded preview proposed one backup-first
  `cleanup_dead_gfid` action. Reviewed execution completed with no failed or
  skipped steps. Independent inspection found the handle absent afterward;
  a fresh preview proposed zero actions.
- **Directory child gap:** Two bricks had the same child directory GFID and the
  third lacked the child. A backend-path preview proposed one
  `recreate_missing_directory_backend` action. After that action completed, a
  fresh preview found the new copy lacked `trusted.glusterfs.mdata`; two source
  copies agreed on its value. A reviewed `attach_directory_mdata` follow-up
  completed. Independent checks then found matching GFID, empty child set and
  `mdata` on all three bricks, and a final preview proposed zero actions.

Both shipped named cleanup paths succeeded. Normal healing was restored,
`gtest3` and `gtest3a` each showed three connected bricks and zero heal entries,
and the VMs were shut down and retained. These two cycles add representative
Gluster 11.1 compatibility evidence for the installed snapshot. They do not
qualify every canary, a naturally persistent 11.1 failure, other distributions,
or the new pre-10 warning code, which was not installed on the guests for these
runs. The beta's advertised repair-write scope remains deliberately narrow.

## Source and companion checks after metadata fix (2026-09-27)

The current 167-file source inventory passed an isolated file-only install
audit: 129 source notices, 70 byte-identical installed runtime files and six
source/installed help invocations. All 135 local core document links passed;
the companion's 45-file inventory and 51 links also passed. The complete core
offline suite exited successfully after the transfer changes; focused
directory/replica tests and the new transfer-command assertions passed.
The companion passed 26 tests and all six skills validated. The external
private-identifier scan found zero current-tree findings in both repositories;
a separate scan of their existing Git patch histories found zero unexpected
patterns, and every existing author/committer identity matched the selected
public identity. These checks precede final reviewed commits and are not a
release tag or publication.
The live nonempty-file result below came from the preceding 166-file copy.
The later change to the 167-file candidate affects directory subtree transfer
and local reference backup commands; the staged-file and mount-restore
commands used in that live result are unchanged. No claim is made that every
167-file repair path was installed and exercised on the VMs.

## First-beta nonempty restore (2026-09-27)

The current 166-file candidate was copied to three disposable Ubuntu 24.04
guests running Gluster 11.1 and installed through the normal bootstrap. The
copied source files and 70 installed runtime files matched the local hashes.
On the replica-3 volume, a synthetic 38,912-byte file had two agreeing source
bricks and one deliberately missing backend copy, with healing suppressed for
the mechanical test. A public CLI preview produced one reviewed
`restore_missing_replica` action.

The first execution preserved bytes, GFID, mode and a `user.beta` xattr, but
changed ownership from UID/GID 0 to 1000 on all replicas. This was a failed
acceptance test. The staging command now runs rsync as root with numeric IDs,
ACLs and user xattrs preserved; it excludes backend `trusted.*` attributes.
The mount restore uses archive copy. A fresh fixture, preview, scratch-stage
probe and one execution then preserved the original digest, GFID, UID/GID 0,
mode 0644 and `user.beta` on all three bricks. Its execution journal marked
the action completed. The earlier fixture's ownership was restored through
the mount and independently checked on every brick. Both volumes were quiet,
normal healing was restored, bounded results exported, and all guests were
shut down and retained. No production volume was changed.

This qualifies one supervised nonempty missing-replica recipe on that exact
lab combination. It does not qualify all repair recipes, ACL-bearing payloads,
other Gluster versions, or native split-brain resolution. The missing target
had no prior contents to roll back. Full suite, fresh-source and privacy audit
results for the final candidate belong in a later entry after source freeze.

A later source review applied the same ownership, ACL and user-xattr transfer
settings to directory subtree staging/push, and archive copying to the local
reference backup. Focused command-preview tests pass. Those directory write
paths were **not** rerun live in this pass and are outside the live-qualified
beta claim.

## Full-heal command smoke (2026-09-27)

On a retained disposable three-guest Gluster 11.1 lab, a connected replica-3
volume had zero pending entries on all three bricks before and after one
`gluster volume heal <volume> full` command. Gluster returned success; peer
links remained connected, a second lab volume stayed quiet, and no active
volume task remained. The guests were shut down and retained after exporting
bounded results. This proves command acceptance on a quiet volume, not repair
of pending entries or split-brain resolution. The full-heal feature flag remains
blocked. The fixed lab helper passed 15 local behavior/refusal tests before use.

## Gluster 11.1 pending-row full-heal observation (2026-09-27)

On the same disposable replica-3 lab, a fresh file was created through the
Gluster mount and checked independently on all three bricks. With one brick
temporarily offline, the mounted file was changed; after the brick returned,
fresh `gluster volume heal <volume> info` output showed the file pending on
two connected bricks (entry counts 1, 1, 0). A manually issued full-heal
command returned success. Subsequent heal info showed zero entries on all
three connected bricks. Independent brick checks found the changed digest,
GFID, UID/GID and mode agreeing on all three copies. The self-heal log recorded
a data heal for that GFID and a full sweep during the same interval.

The self-heal daemon was active, so this run cannot isolate the full-heal
command from concurrent native healing. It proves the command accepted a
volume with a pending row and that the copies converged; it does not prove
that the full command caused the repair or qualify the tool's full-heal launch.
The feature gate remains closed. The exact canary was removed through the
mount, both lab volumes returned to three connected bricks with zero entries,
and the three guests were gracefully shut down and retained. An earlier
fixture attempt stopped before any brick outage or write because the client
mount had not persisted across the VM restart; the successful case used a
fresh one-shot fixture after remounting.

## Shared heal-dispatch gate (2026-09-26)

The shared pending-index and full-namespace heal functions now check the exact
Gluster profile before launching a command. Focused tests proved that unknown
releases and unqualified features dispatch no write; the 11.1 pending-index
path still dispatches. A fresh file-only copy under UID 1000 with isolated
HOME/XDG paths and inherited Python, Gluster, XDG and sudo overrides cleared
matched the 165-file source hashes, 129 local links and source notices. Its
offline suite passed 858 tests in 28.484 seconds with one ACL-dependent skip.
The 70-file installed tree was byte-identical; six source/installed help
commands passed. The external private-identifier scan found zero matches.
No live command qualification was added.
The later operator-hint wording review passed the focused repair-matrix and
status tests (9 tests); it changed no command dispatch or stored artifact shape.

## Compatibility profile audit before shared dispatch (2026-09-26)

The preceding 165-file candidate added exact Gluster feature profiles and strict
post-repair heal-info completion checks. A fresh file-only copy under UID 1000,
with isolated HOME/XDG paths and inherited Python, Gluster, XDG and sudo
overrides cleared, matched all source hashes. It passed 128 local Markdown
links, source notices, entry-point help, and 855 offline tests in 28.590 seconds
with one ACL-dependent skip (`OK (skipped=1)`). A separate installed tree had
70 expected byte-identical runtime files; six source/installed help invocations
passed. The external private-identifier scan found zero matches.

These checks verify local behavior and packaging. Pending index heal has scoped
live proof on Gluster 11.1. Full namespace heal and native per-file resolver
writes remain gated pending their own live command/output qualification; other
Gluster releases need exact-version evidence. See the
[compatibility profile](GLUSTER_COMPATIBILITY.md) and
[open release work](RELEASE_PREPARATION.md).

## Frozen file-only and install audit (2026-09-26)

Against the frozen 2026-09-26 core candidate, a fresh temporary tree copied only the 161
`FILES.txt` entries and ran under UID 1000 with fresh HOME/XDG paths and
inherited Python, Gluster, XDG and sudo overrides removed. Inventory and hashes
matched the committed source; all 120 local Markdown links and source notices
passed. The documented offline suite ran 845 tests in 29.379 seconds with one
ACL-dependent skip (`OK (skipped=1)`). The external private-identifier scan
reported zero findings.

A separate temporary installation contained 69 expected runtime files, each
byte-identical to source; all six source/installed entry-point help invocations
passed. The installed-tree privacy scan also reported zero findings. These
checks cover the frozen local snapshot only; the platform matrix, physical-host
policy and final remediation review remain open.

## Additional scoped qualification (2026-09-24)

Installer failure and post-repair reboot follow-up:

- On the installed three-guest Ubuntu 24.04 lab, changed pinned host identity,
  encrypted operator key without an agent, and failure on the second SCP source
  transfer were refused. For the transfer failure, installed files, sudoers
  policy and the service key were unchanged, and remote staging was removed.
- After the one empty synthetic missing-replica repair, guests rebooted one at a
  time. Each returned with a new boot ID, passed installer/service verification,
  restored all six directed management peer links, and reached connected
  three-brick heal status with zero entries before the next reboot. Final volume
  status showed no active volume tasks. Automated client-mount persistence,
  other distributions and physical-host policy remain untested.
- Invalid package syntax and generated sudoers were refused on the disposable
  guests without changing installed files/ownership/modes, policy, keys or
  remote staging. The offline brick-status and arbiter-role projections passed
  live collection; Gluster 11.1 did not emit an explicit disconnected status
  in `heal_info`, so that field remains unqualified.
- The companion's documented test-discovery command passed all 26 tests; the
  core's documented discovery command now passes all 845 tests, including
  no-copy/refusal and explicit disconnected-state projection regressions.

The preceding core suite passed 843 tests after adding volume UUID binding and
precise backend identity checks. The installed tool in the three-guest Ubuntu lab
accepted unchanged evidence, refused a same-size file change with preserved
mtime, and refused a recreated volume with identical name/brick endpoints but
a new UUID. These are bounded admission checks, not concurrency locks.

A shipped missing-file-replica canary stayed present with healing disabled.
Independent backend discovery found two agreeing copies and one missing copy,
then the reviewed public apply command completed one restore action. Independent
brick checks confirmed the same GFID, size, mode, ownership and content digest
on all three replicas. The fixture was an empty synthetic file: this qualifies
that mechanical operator path, not nonempty payload fidelity or protocol
split-brain recovery. Baseline healing was restored and pending counts were zero.

Sequential reboots exposed a lab name-resolution defect missed by a quiet heal
report. After correcting persistent mappings, all three reboots passed fresh
boot/service checks, every guest's name mappings and management peers, quiet
brick heal before the next reboot, and all six directed service logins.
The temporary client mount was restored separately afterwards; automatic client
mount persistence was not tested. Original failures and reruns remain private.

Ten saved artifacts from the controlled workflow and the three-brick AFR
inspection exported through the metadata-only collector, with zero matches for
the supplied private identifiers and no raw-source or identifier-file copies.
The later live capture also exported an arbiter-volume description and one
offline brick status row. Gluster 11.1 omitted `Status` in `heal_info` for the
offline brick; that explicit disconnected state remains unqualified. Nothing
was sent.

An earlier 161-file copy with fresh HOME/XDG passed 119 local links, source
notices, entry-point help and 843 tests (one temporary-filesystem ACL skip).
That earlier worktree inventory held 161 files with no symlinks; its
full workspace suite passed 845 tests. The earlier updated temporary
install had 69 byte-identical files and no privacy-scan findings; repeat the
file-only/install audit after the source snapshot is frozen.
The companion's 45-file inventory, 26 tests, 50 local links and updated
VM skill validation passed. These checks do not close cross-release/physical
host qualification or final remediation review.

## Earlier preparation snapshot

Local repository preparation rechecked 2026-09-24 after the VM fixes:

- The public inventory now contains 158 files, including the release preparation
  guide. A fresh file-only copy under an unprivileged account, isolated HOME/XDG
  state and cleared inherited overrides passed all three source help commands,
  112 local Markdown links and the source license-notice checks.
- The copy ran 836 tests in 25.223 seconds: `OK (skipped=1)`. One restore test
  skipped because the temporary filesystem rejected its test ACL. The earlier
  inherited-environment run below passed all 836 without skips; privileged
  remote ACL fidelity has separate scoped VM evidence.
- A separate unprivileged temporary installation passed all six entry-point
  checks. Its 68 files were byte-identical to source and its external-identifier
  privacy scan reported zero findings.
- Results and source hashes were retained privately. This result describes the
  tested snapshot before this documentation-only result entry; it is not a
  completed-review tag or final release acceptance. Repeat affected checks
  after any final fixes and follow [release preparation](RELEASE_PREPARATION.md).

VM qualification follow-up checked 2026-09-24 with CPython 3.12.3:

- Fixed remote restore verification to read back through the capture encoding,
  with dry-run deletion reporting to detect missing remote children. Real-rsync
  regressions detect missing data and changed xattrs without modifying either
  comparison tree. Scoped privileged VM recovery and fresh round-trip evidence
  is described in [backup fidelity](BACKUP_FIDELITY.md).
- Fixed canary creation in a fresh operator HOME/XDG tree: prepare and probe
  user-owned state/work roots before creating a fixture, and create mount-parent
  directories without sudo. The initial VM attempt exposed root-owned ancestors
  and failed to save canary state after fault construction. Refusal-before-builder
  and fresh-ownership regressions pass. The VM retry using a newly absent XDG
  state directory succeeded.
- The selected candidate's complete local suite passed 836 tests, no skips, after
  these changes. This inherited-environment run is separate from the earlier
  file-only/fresh-HOME audit and does not replace final inventory/privacy checks.
- Three disposable Ubuntu 24.04 guests passed installation ownership, sudoers,
  service-login/restriction checks, all six directed peer connections, and
  repeat-install account/key preservation. Scope and remaining first-access
  cases are in [bootstrap qualification](BOOTSTRAP.md).
- Three shipped canary builders completed on the replica-3 VM volume: native
  pending metadata, file access ACL with the mismatch online, and directory
  default ACL. Independent heal snapshots then showed zero pending entries;
  installed repair previews passed 38/38 readiness checks and planned no writes.
  These are native-heal-first/smoke and setup/discovery checks, not repair-apply
  proof. The lab was retained powered off; reboot/cleanup qualification is pending.

Earlier checkpoints follow; their remaining-work statements describe those
snapshots, not the later scoped VM results above.

Current selected-tree regression checked 2026-09-20 with CPython 3.12.3:

- `gluster-tests --root <staged repair candidate> all -t 900 -a 120` completed
  successfully: 833 tests passed. The helper records and displays the selected
  root so a candidate cannot silently run the canonical checkout instead.
- This confirms the current candidate's offline test count. It is not a fresh
  file-only copy, deployed-host qualification, or live repair acceptance.

Current in-place inventory checked 2026-09-21:

- The 157-file `FILES.txt` inventory matches the working candidate after
  normalization, with no public-tree symlinks. It contains 113 Python files and
  123 Python/shell source and test files; every source/test file has the
  selected copyright notice and GPL-2.0-only SPDX identifier.
- This is an in-place source-list and notice check. Repeat the privacy scan,
  local-link check, file-only copy, installed-tree check and clean-host work
  against the final frozen inventory before release acceptance.

Current file-only candidate audit checked 2026-09-24 with CPython 3.12.3:

- The first temporary audit's completion output was not retained, so its
  cleanup alone could not establish success. A repeat retained the actual
  process exit, test log, result JSON and per-file source hashes outside this
  repository. The results below refer to that repeat; documentation changes
  recording these results followed the captured source snapshot.
- A fresh temporary tree copied only the current 157 `FILES.txt` entries, with
  no Git metadata or generated bytecode. It used a fresh HOME/XDG state and no
  inherited Python, Gluster, XDG or sudo overrides. It ran under UID 1000.
- The three documented source entry-point help commands, 93 local Markdown
  links and all 123 Python/shell source-test notices passed in that copy.
  The test runner exited 0: 833 tests ran in 25.364 seconds, with one skipped
  (`OK (skipped=1)`). No live Gluster qualification follows from this result.
- The local installer then created the expected 68 runtime files in a separate
  temporary destination. Every installed entry point and package module was
  byte-identical to the staged source, and its installed-tree verifier passed.
- The companion's bounded privacy auditor scanned this candidate with the
  external private identifier list and selected public maintainer credit; it
  reported zero findings without printing the private patterns.
- This rechecks the present staged tree. Repeat it after the final source list
  is frozen, then separately complete installed-host and clean-host acceptance.

## Historical staged notes and remaining requalification

The records below were retained from earlier staging work. They remain useful
for locating the intended tests, provenance, and known limitations. Repeat the
fresh-copy and host-specific checks before release acceptance.

Earlier 153-file staged-candidate audit checked 2026-09-20 on Linux with CPython 3.12.3:

- The 153-file inventory exactly matches the candidate tree, with no listed
  symlinks. All 111 Python sources parse and 84 local Markdown links resolve.
- A file-only copy under UID 1000 with fresh HOME/XDG state passed the README's
  `python3 -m unittest discover -s tests -v` command: 822 tests, one
  ACL-dependent skip. The three documented source help commands passed under
  the same isolated environment.
- The real local installer produced 67 files, each byte-identical to its source
  in the clean copy. Manager, core CLI and worker help succeeded from the
  installed tree outside the checkout. This is local packaging proof only.
- All 123 Python/shell source and test files carry the selected copyright
  holder's notice and a GPL-2.0-only SPDX identifier. COPYING contains the GPL
  v2 text. A source search found no embedded third-party URL, copyright or
  adaptation header. This is an inventory check, not a determination of
  authorship or rights for any external contribution.
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
