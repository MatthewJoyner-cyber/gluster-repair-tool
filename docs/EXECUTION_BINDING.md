# Saved execution origin

New evidence collection records the requested volume name and the queried
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
with the recorded brick hosts, paths and roles. These checks also run with
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
files cannot execute and report that evidence, plan and apply must be rebuilt.
Bare imported observation caches also remain preview-only because they contain
no independently recorded volume origin. Incomplete brick-role evidence cannot
produce an executable binding. No bypass or automatic legacy upgrade is provided.

These are consistency checks, not signatures against a malicious author who
can rewrite the whole bundle. They bind saved object identities and contents;
they do not re-read every live inode, checksum, ACL or xattr. The volume name
and endpoint comparison does not detect a recreated volume with exactly the
same name, endpoints and roles. Nor does it lock cluster topology after the
check. Per-action freshness guards, verification and conservative handling of
unknown writes remain necessary. Other findings in the pre-export review
still block release and live qualification remains pending.

Run artifacts contain operational paths and evidence. Keep them in private
state storage as described in [the privacy policy](PRIVACY.md).
