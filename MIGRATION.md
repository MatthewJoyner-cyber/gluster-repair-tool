# Migration to separate repair-tool and agent repositories

## Destination model

1. Keep the original repository private and reference-only after cutover.
2. Create a fresh repair-tool repository from the sanitized core candidate.
3. Create a separate fresh agent repository from the companion candidate.
4. Keep personal steering and operational ledgers under the user's home
   directory, outside both repositories.

This guide records the split from a private predecessor into two independent
repositories. No predecessor Git database, remotes, old tags, author identities,
or original ledger was imported. The repair-tool repository has its own
sanitized development history under the author's chosen public identity.
Confirm the chosen public author identity before any future commit.

Repository destinations are in [MAINTAINERS.md](MAINTAINERS.md); follow the
[release preparation sequence](docs/RELEASE_PREPARATION.md).

## What belongs where

| Material | Destination |
| --- | --- |
| Repair engine, CLI, tests, public usage docs | Repair-tool repository |
| Portable agent skills, adapter instructions, privacy audit tooling | Agent repository |
| Frozen summarized implementation prehistory | Core HISTORY.md |
| Future implementation details | Commit messages in the repository changed |
| Product bugs and release tasks | Each repository's steering/TODO.md |
| Personal preferences, checkout mappings, helper/host configuration | External private steering |
| Detailed incidents, live runs, personal tasks and operational history | External private ledger |
| Original Git history and full old ledger | Private reference repository |

The default private root is `$HOME/.gluster-repair-private`, with separate
`steering/` and `ledger/` directories. An agent may use the documented
GLUSTER_REPAIR_PRIVATE_ROOT override. The core does not load personal steering.

## Before cutover

- Freeze the exact source revision and preserve uncommitted review work in a
  private checksum inventory. Keep source provenance and identifier mappings
  private.
- Copy only the reviewed candidate file list. Never clone, mirror, bundle,
  filter, or copy the old .git database into either new repository.
- Resolve the open core review and run its required fault/integration tests.
  The agent cannot compensate for missing engine safeguards.
- Review all READMEs, code comments, tests, examples, filenames, and generated
  artifacts. Public examples must be synthetic. Required license notices and
  public citations remain intact.
- Run the privacy audit against the exact candidate trees and a private
  local-identifier list. Inspect any finding and scan again after remediation.
- Qualify fresh installation and document supported Python, Linux, Gluster,
  and adapter versions. Current source tests are not deployed-host acceptance.
- Both candidates use GPL-2.0-only and identify their selected public
  copyright holder in MAINTAINERS.md. Review [source provenance](PROVENANCE.md)
  before adding any external contribution.

## Establish the new histories

Use separate directories for the repositories. Preserve an explicitly authorized
sanitized development history when qualifying that candidate; otherwise start
with empty destinations. Copy accepted trees without caches,
private imports, runtime output, symlinks to personal files, or VCS metadata.
Inspect filenames before Git initialization.

Initialize each independently and configure a deliberately chosen public author
name/email. Inspect the staged content and the proposed identity before the
initial commit. Record the sanitized provenance summary, validation, and known
limitations in that commit; keep private commit hashes and mappings external.
Select remotes and publish only with explicit user authority.

Do not copy the old ledger or introduce a replacement running implementation
ledger. HISTORY.md remains a frozen pre-migration summary. Future commits use:

```text
Short title describing the behavior change

Problem:
Concrete trigger or defect.

Change and rationale:
What now happens, and why this approach was chosen.

Validation:
Commands/results and whether evidence is synthetic, local, or disposable-live.

Caveats:
Remaining limits, follow-up tasks, and compatibility effects.
```

Keep personal details out of commit bodies too. Record a private live-run
reference in the private ledger and put only its sanitized conclusion in the
public commit. Rework commit titles/bodies around final behavior rather than
copying conversation history.

## Runtime and user-state cutover

The candidate uses per-user state defaults for work and backup discovery.
Do not move active runs, archives, or mounted work directories automatically.
Before switching, inventory old state privately, set explicit work/backup
overrides if retaining those locations, and verify the intended status and
archive paths. Unknown prior writes require verification before any resume.

Install the companion separately using its reviewed adapter instructions.
Do not copy an existing user's Codex configuration, trust rules, SSH keys,
installed helper inventory, memories, or personal skills wholesale.
The core must run offline tests and show help with the companion and private
folders unavailable.

## After cutover

Mark the original repository reference-only operationally; retain it for
private lookup and do future development in the two new repositories. Record
the final destinations in private steering. Do not rewrite or delete history
to pretend the reference repository is public-safe.

For subsequent reviews, use only the new repository's completed-review tags.
Create the first such tag after scoped fixes are implemented and validated;
do not carry old tags forward. Keep detailed change/validation/caveat records
in each commit, and update current docs and TODO alongside code.

## Maintainer identity and hosting

Maintainers may publish chosen aliases and explicitly supplied public contact
information. Follow MAINTAINERS.md; do not derive public identity from private
configuration. GitHub account creation, remotes, pushes, and publication wait
until the owner is ready and explicitly authorizes them. Staging is local only.
