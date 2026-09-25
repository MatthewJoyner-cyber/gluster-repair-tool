# Diagnostic collection qualification

The support-bundle command only reads explicitly named, already saved
artifacts. It does not collect data from a brick, start a heal, or send a
report. This checklist defines the supported collection boundary and the
evidence required before the diagnostic-format TODO item can be closed.

## Supported artifacts

| Bundle label | Bounded saved source | Current local coverage | Live qualification needed |
| --- | --- | --- | --- |
| `heal_info` | `gluster volume heal VOLUME info` captured without starting a crawl | Connected three-brick result exported from the disposable replica-3 volume; rows, GFIDs and explicit connected/disconnected flags covered by schema tests | Capture an explicit disconnected status section. Gluster 11.1 omitted the status field for its offline brick. |
| `volume_info` | `gluster volume info VOLUME` | Replica-3 and replica-3-arbiter layouts captured from disposable volumes; data/arbiter roles exported | Other Gluster releases remain unqualified. |
| `volume_status` | `gluster volume status VOLUME` | Online and one offline brick row captured and exported from the disposable replica-3 volume | Other Gluster releases remain unqualified. |
| `health` | Version-1 `health-check` JSON report | Real report from the disposable topology exported with selected readiness fields | Other report versions remain unqualified. |
| `manifest`, `observations` | Focused `repair-meta` or `evidence-build` writer output | Real read-only writer output projected with cross-artifact aliases | Broader workflow shapes remain unqualified. |
| `plan`, `apply`, `execute_results`, `status` | Tool writer outputs from a disposable workflow | Real controlled-workflow outputs projected with outcome relationships | Broader action types remain unqualified; do not rerun an unresolved repair merely to populate a bundle. |
| `afr_inspection` | Canary worker's read-only inspector result | Read-only inspector results from all three bricks for the canary-owned path; only 12-byte AFR counters retained | Other releases remain unqualified. |

## Acceptance procedure

1. Create a private identifier file outside the public checkout and collect
   only the saved artifacts in the table.
2. Build the bundle locally, inspect every exported JSON file and
   `inventory.json`, then verify the source and identifier file are not copied.
3. Check that hostnames, paths, volume names, action IDs and numeric ownership
   values are aliases, and that messages, command text, logs, file contents,
   credentials and unknown fields are absent.
4. Record the installed Gluster version, input command forms and artifact labels
   in private operations records. Do not record server names, addresses, paths,
   credentials or payloads in the public repository.
5. Record recognized labels with unsupported schemas as
   `omitted_unsupported_format`; unknown labels must be rejected before reading
   their files. A new format needs a versioned schema, hostile-input tests and
   reviewer approval; it must never enable a raw-copy fallback.

The disposable Ubuntu 24.04 lab exported ten saved workflow artifacts, a live
replica-3-arbiter volume description, an offline brick status row and the
three-brick AFR inspection with the external identifier scan passing. Unknown
labels, including logs and worker wrappers, are rejected before artifact reads;
recognized labels with unsupported schemas are inventoried as omitted. An
explicit disconnected `heal_info` status remains unqualified because Gluster
11.1 omitted that field for the offline brick. Nothing was submitted.

Passing the local tests establishes only the projection contract. It does not
qualify a particular Gluster release, prove anonymity, or authorize a repair or
support submission.
