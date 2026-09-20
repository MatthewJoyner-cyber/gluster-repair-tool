# POSIX canary source-choice bridge

`file-posix-metadata-split-brain-build` converts a saved canary state into a
review plan for choosing a POSIX metadata source. The state is a harness
artifact, not an operator observation or permission to execute a repair.

The current bridge accepts only version-1 `x3-upstream-derived-fixture` state
with a matching volume/scenario identity, complete three-brick backend and
metadata maps, supported data/arbiter roles, a data-brick source, and a
canonical recorded GFID. Its flags must say that construction completed and a
heal crawl ran. The captured split-brain output before and after the crawl must
each contain the exact volume-relative path or exact canonical GFID on a
recorded brick. Generic nonempty output, a same-basename row, a setup boolean,
or an AFR pending marker cannot substitute for those rows.

Legacy states without this version and evidence must be retained for diagnosis
or cleanup, then rebuilt from a supported fixture or fresh operator evidence.
Direct replica-4 bookkeeping fixtures and pending-only fixtures remain
diagnostic. A newly created pending fixture records that source choice is
ineligible even when a saved row exists. The canary creator also refuses an
arbiter source before mounted or brick-side mutation.

This check uses saved CLI snapshots; it does not establish that the row, GFID,
topology or metadata is still current. Later planning and execution need their
own fresh evidence and binding checks. The canary capture helper also uses
[exact heal-row matching](HEAL_ROW_MATCHING.md); the bridge adds its own
saved-state provenance and topology checks before admission.
