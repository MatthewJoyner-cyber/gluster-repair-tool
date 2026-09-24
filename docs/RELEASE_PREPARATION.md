# Repository release preparation

The local candidates are being prepared before the first GitHub push. The
owner-approved destination names are recorded in [MAINTAINERS.md](../MAINTAINERS.md);
they are planned destinations, not a claim that either repository is online.
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

## Final pre-push checkpoint

1. Finish the remaining scoped qualification listed below and address findings.
2. Freeze the exact source snapshot; repeat inventory, source notices, privacy,
   local links and relevant tests. Inspect generated files and the Git history
   separately because the source privacy scan excludes `.git`.
3. Review the final diff and public author/committer metadata, then commit when
   authorized with problem, change, rationale, validation and caveats.
4. Create the first completed-review tag only after its remediation is
   implemented and validated. Record the reviewed scope; a tag alone is not
   acceptance of every platform or repair case.
5. When the owner authorizes publication, create the two empty GitHub
   repositories, verify each destination, and push only each clean repository's
   reviewed branch and deliberately selected tags. Recheck cross-repository
   links once the destinations exist.

Read [MIGRATION.md](../MIGRATION.md) for source/state cutover. Final tests may
require fixes; rerun the checks affected by those changes before publication.

## Core qualification still open

- Live execution-origin binding, including same-name volume recreation and
  object changes: [execution binding](EXECUTION_BINDING.md).
- Metadata-only diagnostic collection in intended operator workflows:
  [collection qualification](DIAGNOSTIC_COLLECTION_QUALIFICATION.md).
- Stable repair-apply proof, post-volume reboot and remaining installation
  failure cases: [bootstrap](BOOTSTRAP.md) and [test plan](../steering/TEST_PLAN.md).
- Supported environment claims must remain within the tested matrix:
  [portability](PORTABILITY.md).
- Repeat file-only and installed-tree checks against the final snapshot and
  complete the remediation review: [TODO](../steering/TODO.md).

Scoped Ubuntu installation, privileged backup and native-heal canary results
are recorded in [VALIDATION.md](VALIDATION.md). They do not close the gates above.
