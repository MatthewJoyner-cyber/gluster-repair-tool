# Repository release preparation

The [first-beta release plan](FIRST_BETA_RELEASE_PLAN.md) defines the current
acceptance scope and supersedes treating every historical qualification gap
as a first-publication blocker. This is a best-effort beta, tested on the stated
Ubuntu LTS baseline, with no exhaustive-coverage or reliability guarantee.

The separate GitHub destinations are recorded in
[MAINTAINERS.md](../MAINTAINERS.md). Apply these checks to the exact source
revision selected for publication or a later prerelease.
The original repository and its ledger remain private reference material.

## Local preparation

- Keep an exact `FILES.txt` inventory of public source, tests and documentation.
  Exclude Git metadata, caches, runtime evidence, private settings and credentials.
- Keep GPL-2.0-only notices, chosen maintainer credit and source provenance
  consistent. Preserve the frozen sanitized `HISTORY.md`; record future
  implementation detail in commit bodies rather than a replacement ledger.
- Use an independently initialized Git history and the deliberately selected
  public author identity. Preserve authorized sanitized development checkpoints;
  never import the predecessor's Git database or tags.
- Review README commands, relative links and current qualification statements.
  Run the offline suite and the privacy scan with the external identifier list.
- Keep remote creation and push separate from local preparation. The companion
  remains optional; the core must work without its skills or private settings.

## Publication checkpoint

1. Complete checkpoints A-C in the first-beta plan: review known safety fixes,
   one focused current-candidate pass, and the final source/companion audit.
   Keep optional unqualified features disabled; do not wait for every platform
   or historical canary to be qualified.
2. Freeze the exact source snapshot; repeat inventory, source notices, privacy,
   local links and relevant tests. Inspect generated files and the Git history
   separately because the source privacy scan excludes `.git`.
3. Review the final diff and public author/committer metadata, then commit when
   authorized with problem, change, rationale, validation and caveats.
4. Create the first completed-review tag only after its remediation is
   implemented and validated. Record the reviewed scope; a tag alone is not
   acceptance of every platform or repair case.
5. Verify both GitHub destinations, push only clean reviewed branches and
   deliberately selected tags, and check cross-repository and public-download
   links before announcing the release.

Read [MIGRATION.md](../MIGRATION.md) for source/state cutover. Final tests may
require fixes; rerun the checks affected by those changes before publication.

## First-beta acceptance and later qualification

Installed execution-origin refusal, fresh Ubuntu installation, scoped privileged
backup fidelity and diagnostic collection already have live results in
[VALIDATION.md](VALIDATION.md). Review those results against changed code rather
than restarting completed qualification from historical open-task wording.

The bounded nonempty missing-replica workflow passed on 2026-09-27 after a
staging ownership defect was found and fixed; see [validation](VALIDATION.md).
Before a repair-capable beta, finish the final remediation/source review. Keep
environment and repair claims within that evidence. The
[test plan](../steering/TEST_PLAN.md) identifies the existing checks.

Full namespace heal and native resolver writes remain disabled. Their additional
qualification, broader diagnostic schemas, other Gluster releases and physical
host/distribution coverage are follow-up work, not first-beta blockers while
excluded from the advertised scope. Other Linux distributions are untested;
the public test claim is Ubuntu LTS. Missing connection state remains unknown
and cannot establish successful post-repair completion.
