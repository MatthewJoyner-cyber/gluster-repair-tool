# Saved execution origin

New evidence collection records the requested volume name, Gluster volume UUID and the queried
brick endpoints with their data/arbiter roles. Plans inherit that origin from
the manifest. Apply artifacts inherit it from the plan. A mutable status file
cannot supply a replacement origin at either build or execution time.

The `origin_binding` field has its own schema version and SHA-256 fingerprints.
It binds the artifact's contents, including action identities, step targets,
decisions and controller-cycle metadata, to the source plan and evidence.
Source JSON fingerprints are insensitive to formatting and object-key order.
The evidence and plan must remain available at their recorded paths; changing
or moving them requires rebuilding the dependent artifacts.

All public `apply-run` forms use the package CLI's shared execution lifecycle.
On `--execute`, it validates the binding before health
refresh, heal-policy changes or execution. They compare fresh volume information
with the recorded volume UUID, brick hosts, paths and roles. Recreating a volume
with the same name and endpoints changes its UUID and requires fresh artifacts.
These checks also run with
`--skip-health-check`. If health is refreshed, the binding and live topology are
checked again before execution proceeds. Health and heal operations use the
bound volume. A status file pointing at different evidence or a different plan
is refused even when the volume name matches.

Controller-stop decisions from either status or the saved apply artifact block
execution. Snapshot requirements, batch authorization, heal policy and action
validation apply to plain manager commands as well as ready-only and managed
heal forms. Invalid actions are rejected before changing heal settings. During
post-execution refresh, temporarily enabled heal options are restored to their
previous values; failed restoration blocks completion. An interrupted executor
records an unknown write state and requires inspection before retrying.

Filtering selects actions from the validated in-memory artifact; it does not
reload another copy from disk. An explicitly planned continuation inherits the
validated source lineage and gets a new content fingerprint. Fresh replanning
records new evidence rather than relabelling the old apply artifact.

Old manifests, plans and apply files remain usable for preview. Unbound apply
files, including older bindings without a volume UUID, cannot execute and
report that evidence, plan and apply must be rebuilt.
Bare imported observation caches also remain preview-only because they contain
no independently recorded volume origin. Incomplete brick-role evidence cannot
produce an executable binding. No bypass or automatic legacy upgrade is provided.

Before repair dispatch or heal-policy writes, selected actions also trigger a
fresh backend observation through the deployed worker. The check compares
saved backend evidence, including GFID, inode, nanosecond modification/change
times and collected metadata. Missing, duplicate, errored or changed evidence
requires rebuilding the manifest, plan and apply artifacts. Existing objects
without precise identity fields in older manifests remain preview-only.
Mount probes are excluded from this read-back because a lookup can trigger
native healing. The probe reads metadata, not file contents.
Every bound brick needs an observation for the selected object. Multiple bricks
on one host are refused at execution until evidence can distinguish them;
the current observation map is keyed by host.

These are consistency checks, not signatures against an author who can rewrite
the whole bundle. They do not checksum file contents or lock topology and
objects against changes after the check. Keep clients quiescent during repair;
per-action guards, verification and conservative handling of unknown writes
remain necessary. On the disposable Ubuntu/Gluster lab, the installed CLI
refused a recreated volume with identical names/endpoints and a changed file
whose size and modification time were preserved. Unchanged backend metadata
passed the probe. These refusal checks do not establish repair-apply acceptance.

Run artifacts contain operational paths and evidence. Keep them in private
state storage as described in [the privacy policy](PRIVACY.md).
