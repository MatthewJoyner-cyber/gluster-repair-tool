# First beta release plan

Planning baseline: 2026-09-27. This plan covers the first public core and
optional companion releases. It defines a finite acceptance scope; it is not
a declaration that acceptance has completed. The operator resumed release work
after the earlier lab pause. Follow the live checklist before claiming acceptance.

Current checkpoint: the one nonempty missing-replica test passed on a fresh
fixture after a staging ownership defect was found and fixed. The first failed
run and the successful retest are recorded in [VALIDATION.md](VALIDATION.md).
Two later, shipped operator-seeded canaries completed ghost-handle cleanup and
a two-stage directory repair on Gluster 11.1. They broaden representative
compatibility evidence without expanding the beta's advertised write scope.
Normal healing is restored and the retained guests are shut off. Local scope,
source and privacy reviews passed and are marked by signed review tags in both
clean repositories. GitHub publication and public-download checks remain open.

## Release objective

Publish a useful, operator-supervised beta for evidence collection, reviewable
repair planning and narrowly evidenced recovery, then improve it from reports.
Do not wait for every canary, Linux distribution or Gluster defect to be tested.
The readiness goal is practical completeness of the chosen scope, not a measured
99% success rate, data-safety probability or coverage percentage.

This is a best-effort open-source release with no guarantees; the warranty
terms are in [COPYING](../COPYING). Reasonable testing and transparent limits
are the acceptance standard. Neither exhaustive coverage nor proof that every
possible failure is prevented is required. Known serious defects still need
a fix or exclusion of the affected feature from the release scope.

Public designation: version 0.1.0, explicitly marked beta. The privately
staged `v0.1.0-beta.1` tags mark the earlier candidate; use
`v0.1.0-beta.2` for the final README and metadata revision in each repository.
Verify CLI, plugin and release metadata agree on this designation before tagging.
The core works independently; the companion explains and invokes its interfaces.

The intended audience is administrators willing to review evidence and plans,
retain backups and supervise writes. Broad unattended recovery, arbitrary
split-brain resolution and every implemented repair recipe are not beta claims.

## Platform and version policy

The public claim is **tested on Ubuntu LTS**, with the exact tested combination
listed. Other Linux distributions are untested unless separately qualified.
Accept feedback from SUSE/SLES, openSUSE, Red Hat Enterprise Linux and other
major distributions without implying those environments were tested.

| Combination | First-beta status | Next action |
| --- | --- | --- |
| Ubuntu 24.04 LTS, amd64, Python 3.12.3, Gluster 11.1, XFS lab bricks, replica 3 | Primary tested baseline; one nonempty missing-replica restore passed after a defect fix; final candidate checks still required | Complete the finite checklist below |
| Replica-3 arbiter on the same Ubuntu baseline | Scoped topology/diagnostic evidence only | Preserve arbiter payload-source refusal; do not imply repair acceptance |
| Ubuntu 26.04 LTS with distribution Gluster 11.2 | Next qualification target; untested by this tool | Review interface/packaging differences, then run the focused compatibility suite after beta 1 |
| Other maintained Ubuntu LTS combinations | Not automatically qualified | Add only when useful to users and independently tested |
| SUSE/SLES, openSUSE, RHEL and other Linux distributions | Untested; best-effort report triage | Record exact OS, package source/version, Python and policy differences before diagnosing |

Current Ubuntu package listings show Gluster 11.1 in
[24.04 LTS](https://packages.ubuntu.com/noble/glusterfs-server) and 11.2 in
[26.04 LTS](https://packages.ubuntu.com/en/resolute/amd64/admin/glusterfs-server).
Recheck package revisions and [OS lifecycle](https://ubuntu.com/about/release-cycle)
when selecting a new test image. Prefer maintained distribution package streams;
do not recommend downgrading a working cluster to fit this tool's matrix.

Separate OS maintenance, Gluster package maintenance, and this tool's tested
compatibility. Ubuntu lists Gluster in Universe; its security support terms
differ from Main ([Canonical explanation](https://ubuntu.com/security/esm)).
RHEL support does not revive Red Hat Gluster Storage, which reached EOL on
2024-12-31 ([vendor policy](https://access.redhat.com/support/policy/updates/rhs)).
Verify the Gluster package provider's maintenance status for any SUSE or RHEL
deployment instead of inferring it from the OS brand. No vendor-support claim
is made by this project.

Compatibility review should concentrate on interfaces the tool uses: command
arguments, output parsing, package/helper layout, privilege requirements,
identity/xattr semantics and execution outcomes. Internal fixes normally need
a note, not a new recovery experiment. A security or data-integrity change that
invalidates a tool assumption can still matter without changing CLI syntax.

Keep the current exact-version feature gates for beta 1. A different version
being unqualified does not prove it incompatible. Future admission should use
the relevant contract checks and a representative live smoke, not replay every
old Gluster bug. Do not silently enable all 11.x releases based on their numbers.

## Evidence already available

Use [VALIDATION.md](VALIDATION.md) for dated evidence, not historical open-task
wording in the predecessor review. Confirm source changes since each proof;
reuse a result if its implementation and environment remain relevant.

| Area | Evidence already recorded | Beta consequence |
| --- | --- | --- |
| Core offline behavior and packaging | 858 tests on the latest recorded full audit, one ACL-dependent skip; isolated state, source hashes, installed file parity, help and privacy checks | Repeat once on the frozen final candidate; explain the skip |
| Installation and service restrictions | Three fresh Ubuntu guests, real sudoers, six directed SSH logins, reinstall and installer failure cases | Do not rebuild a fresh OS just to repeat unchanged installation behavior |
| Execution identity | Installed CLI refused recreated-volume identity and changed data with preserved size/mtime | Review the fix and current refusal tests; no new volume-recreation experiment unless affected code changes |
| Backup maintenance | Privileged remote archive/verification/restore preserved data and metadata, including ACLs, trusted xattrs and hardlinks | Reuse this proof; distinguish it from each repair recipe's original backup/rollback steps |
| Mechanical repair | One nonempty missing-replica restore passed on a fresh fixture after an ownership defect was fixed; later operator-seeded ghost and directory cycles reached zero-action previews | Advertise only the supervised nonempty restore as beta repair-write scope; retain the other cycles as compatibility evidence |
| Native healing | Three recent metadata/ACL fixtures left zero pending entries after native healing | Record no remaining repair case observed on this setup; no forced repair required |
| Full namespace heal | Gluster 11.1 accepted commands on a quiet volume and with a pending row; copies converged in the latter run | Concurrent native healing prevents attributing the repair to the full command; tool launch remains disabled for the first beta |
| Native split-brain resolver | Synthetic success/tie/error and no-fallback tests; no live write qualification | Leave disabled for beta 1; remove its live qualification from release blockers |
| Diagnostic projection | Ten saved workflow artifacts, AFR reads, arbiter topology and offline brick status; identifier scans passed | Use existing schemas; missing heal status stays unknown, not a reason to find another release for beta 1 |
| Companion | 26 local tests, six-skill validation/lifecycle and recorded answer scenarios | Rerun affected guidance scenarios; repeat lifecycle only for relevant adapter/CLI changes |

The nonempty restore used a complete 166-file installation. The later 167-file
candidate changed directory transfer and backup commands, outside that live
repair path. The subsequent pre-10 health warning was tested offline but was
not deployed to the guests. Do not describe those later changes as live repair
acceptance.

## Rationalized item 1: focused live acceptance

Item 1 formerly accumulated full-heal effects, resolver variants, historic bug
reproductions and platform expansion. For beta 1 its purpose is to check the
advertised tool workflow on the declared baseline. Complete it as follows.

1. **Freeze a candidate and establish the baseline.** Reuse the retained lab
   after checking identities and current health. Deploy the exact public file
   inventory through the shipped installation path and compare installed
   hashes on controller/workers. Record OS, package revisions, Python, brick
   roles, volume identity, heal settings and peer state. Do not mix selected new
   modules with a stale controller. Existing fixtures and evidence are preserved.
2. **Let native recovery finish first.** In the ordinary operator workflow,
   assess health and backup readiness, complete appropriate Gluster-native
   recovery (including an administrator-authorized full heal when appropriate),
   then recollect evidence. Tool-managed full-heal dispatch remains gated;
   this does not declare the separate Gluster administration command broken.
   Do not repeat a completed full heal just to populate a test record.
3. **Accept the no-repair result.** If native healing clears a canary, verify
   independent heal/object evidence and that the tool proposes no unnecessary
   write. Record "not reproduced after native healing on this tested setup."
   Claim an upstream fix only with an upstream reference and matching evidence.
   Reuse the three recent native-heal observations rather than recreating them.
4. **Run one representative remaining repair.** Select an existing understood
   recipe for one nonempty synthetic file on replica 3, with independently
   established source agreement and GFID. Record any deliberate healing
   suppression used to keep the lab fixture stable; that is mechanical repair
   proof, not a naturally persistent post-heal incident. Collect fresh evidence
   through the public CLI, review the plan and backups, execute once, and verify
   bytes/digest, GFID, ownership/mode and applicable ACL/xattrs on every replica.
   Inspect the attempt journal and backup, and exercise that recipe's rollback
   on disposable data if rollback is part of its advertised behavior. Restore
   ordinary healing and verify convergence. Do not reuse a completed apply file.
5. **Check refusal and diagnostics on the frozen candidate.** Run existing
   tests for stale identity, unavailable/disconnected evidence, unknown outcomes
   and disabled commands. Use one installed read-only preview and metadata-only
   export from the current workflow; verify omitted fields, aliases and absence
   of payloads/private identifiers. Reuse unchanged live fault evidence; do not
   inject every failure again. Unknown outcomes must stop further writes.
6. **Close the run.** Independently check peers, bricks and both lab volumes,
   restore recorded settings, export bounded evidence, gracefully shut down and
   verify retained guests are off. Publish only sanitized conclusions.

Keep this to one bounded qualification pass with focused diagnosis if it fails.
Do not keep inventing fixtures to produce a failure that Gluster already heals.
A failed safety assertion blocks the affected write path. If a useful repair
path cannot be qualified promptly, explicitly reduce and enforce the released
execution scope or prepare a diagnostics/planning-only beta; do not label it
repair-qualified merely to close the checklist.

### Tests deliberately removed from item 1

- Reproducing every 11.0/11.1 release-note bug, rebalance/linkfile behavior,
  snapshots, geo-replication, EC/disperse layouts and lock-recovery internals.
- Full-heal pending-effect and native resolver success/tie/failure experiments
  while those tool features remain disabled. These become feature-enablement
  work after beta feedback, with their own tests before enabling writes.
- Hunting for an explicit disconnected `heal_info` field that this 11.1 setup
  omits. Current unknown-state handling and refusal are the required behavior.
- The failed-directory-rename probe. Its earlier stop-before-rename was a
  harness GFID-path defect; after correction it passed as a scoped native
  Gluster 11.1 observation. It is outside the first-beta repair acceptance path.
- Repeating the complete installer/reboot matrix on unchanged code, new physical
  hardware or every Linux family. Add targeted tests when a relevant change
  or actual report justifies them.

## Four release checkpoints

### A. Review and lock the beta scope

Review R1-R13 remediation against the current code and evidence. Close stale
open wording for already completed identity, installation and backup checks.
For each exposed write path, state its evidence and experimental limitations;
confirm disabled features cannot be reached through alternate CLI forms.
Do not infer that the Gluster profile gates every backend mutation: currently
it explicitly gates heal/native-resolver commands, not every repair action.

Exit: no known unresolved data-loss, wrong-target, unsafe fallback/replay or
privacy defect in the released scope; scope and visible capability/refusal
messages agree. Optional coverage gaps have explicit deferrals below.

Review result, 2026-09-27: R1-R13 implementation records were checked against
the current source and passing offline suite. R1 has installed UUID/data-change
refusal evidence; R2-R5 have current gate, dependency, unknown-outcome and
journal regressions; R6-R8 have scoped backup, installer and host-helper lab
evidence; R9-R10 retain conservative canary/row matching; R11-R12 keep support
export metadata-only and report evidence logs; R13 has the isolated source and
Ubuntu installation checks. The nonempty missing-replica live run exposed an
ownership defect, which was fixed and retested on a fresh fixture. A source
read found analogous transfer commands in directory paths; those now preserve
numeric ownership, ACLs and user xattrs, with command tests passing. The
directory change has no live repair-apply qualification.

The beta's *live repair claim* is only the supervised nonempty file
missing-replica recipe on the listed Ubuntu/Gluster combination. Other repair
recipes remain accessible for expert review and disposable testing but are
experimental; a ready preview is not evidence that a recipe was live-tested.
The exact-version profile admits pending index heal on 11.1, while tool-driven
full heal and native per-file resolver writes remain blocked at shared dispatch.
This review found no remaining known data-loss, wrong-target, replay or privacy
defect in the claimed path. It does not prove every exposed recipe safe, nor
does it remove the need for operator review of current evidence and backups.

### B. Complete the focused current-candidate pass

Perform item 1 above when lab work is resumed. Retain the exact candidate ID,
test outcomes and private evidence pointers. The claimed repair scope must
match the result. A passing empty-file or native-heal check is not nonempty
repair proof. A deferred feature does not fail the release if it stays disabled.

### C. Freeze and audit both source releases

- Run the complete core offline suite once on the frozen source, relevant
  shell/Python checks, clean HOME/XDG help and byte-identical installed-tree
  checks. Rerun only affected checks after subsequent changes.
- Run companion tests, manifest/skill validation and the use-tool,
  native-heal-clears-case, replica-3 setup, optional support-draft and
  unsupported-distribution scenarios. Answers must stay inside the core scope.
- Reconcile README, compatibility table and release notes: tested Ubuntu
  combination, beta limitations, current disabled commands and feedback route.
  Keep raw server file contents out of diagnostic bundles by default.
- Check inventory, local links, source notices, GPL-2.0-only provenance,
  chosen public credits, source privacy and the complete new Git histories.
  Preserve the old repository and detailed private ledger outside publication.

Exit: no unexplained failure, privacy finding, stale release claim or mismatch
between tested source and release source. Prior passing counts are historical,
not a substitute for recording the final run.

### D. Publish and invite bounded feedback

Record the completed review scope and create an annotated review tag in each
clean repository after fixes pass. Create the two selected GitHub repositories
under existing publication authority after acceptance, verify destinations,
push only reviewed branches and selected tags, then create clearly labelled
beta prereleases. Link companion/core tested versions in release notes.
Check public links and source download/help after publication; a source-copy
simulation alone did not test the real GitHub download.

Invite reports about setup, unclear decisions, parser/output differences and
remaining repair cases. Request tool version, OS/package version and provider,
topology class, expected/actual behavior and a reviewed optional metadata bundle.
Use synthetic aliases; request no credentials, private topology or server file
contents by default. Do not upload or submit reports automatically.

For early feedback, triage a possible data-loss or privacy issue before feature
requests, suspend the affected write feature if necessary, and retain prior
release evidence. Publish small corrective betas. Plan the next matrix extension
from actual demand, starting with the newer Ubuntu LTS package stream.

## After beta 1

Prioritize Ubuntu 26.04/Gluster 11.2 contract and installation qualification,
then feature-enablement evidence for full-heal/resolver commands if useful.
Broader payload/recipe coverage, Python versions, physical-host policy, other
Linux distributions and additional diagnostic schemas remain separate work.
SUSE and RHEL feedback is welcome without a promise to test every Linux surface.

Review package lifecycle and interface assumptions at each release and when
distribution packages change. Keep an explicit tested matrix and known limits;
do not hold the first beta for optional coverage once checkpoints A-D pass.
