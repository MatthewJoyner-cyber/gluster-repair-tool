# Maintainer diagnostic evidence

After publication, users may choose to send a diagnostic case to the Gluster
Repair Tool maintainers. The collection tool prepares a local bundle; it never
sends one. The intended evidence is metadata such as object-name aliases, sizes,
GFIDs, brick roles, heal rows, AFR state, and command outcomes. Do not include
server file contents, payload samples, credentials, or unrelated files.

The repair planner classifies *observations*. `review_dead_gfid_reference` is a
review decision, not proof that a live canonical GFID handle is broken. An
operator GFID, GFID-child, backend-path, or index-entry seed can use
`localhost` as input provenance; that name is not a brick or stale-index proof.

Focused operator discovery now reads current `gluster volume heal ... info`
without starting a crawl and carries matching GFID split-brain annotations
into the manifest. A split-brain row with no GFID identity, or a failed heal
snapshot, blocks operator index cleanup. A matching GFID row also blocks it.
The stale-index proposal needs exact observed index paths and, for bare GFID
entries, proof on every queried brick. This is a conservative local check;
the saved snapshot can age, and live qualification remains open. A path-only
split-brain row is treated as incomplete identity evidence rather than matched
to a different object by pathname guesswork.

The canary worker already has a bounded, read-only `inspect_afr_state` operation
for a canary-owned backend path. It reads `trusted.afr.*` names and values
without xattr writes. Collect its existing output through the canary worker
route when the canary and authorized host access are available. The ordinary
host helper's narrower `getfattr` interface is not evidence that this worker
operation is absent. A maintainer diagnostic report may also include existing resolver
records, heal output, volume info/status, role evidence and relevant run
artifacts. If an artifact was never collected, list it as missing; do not
repeat a destructive or unresolved repair merely to fill a bundle.

`simple` mode's handoff lists paths as **present** or **missing**. It contains
raw names and paths and must not be shared directly. To make a private,
pseudonymized bundle from already saved metadata artifacts, create a private
identifier file. Give one entry per line, such as:

```text
server:actual-brick-name
organization:actual-organization-name
person:actual-person-name
account:actual-login-name
path:/actual/private/root
secret:actual-secret-value
```

The exporter assigns stable `serverN`, `pathN`, `volumeN`, `actionN`, `stepN`,
`uidN`, `gidN` and `afrN` aliases across one bundle. An IP used as a host becomes
a server alias too. Entire paths and filenames are replaced, including names
used as JSON keys. Identical strings in the same category share an alias;
different spellings are not inferred to identify the same host or object.
The private reverse mapping is never written into the bundle. Aliases are
independent between bundles and do not preserve path hierarchy.

The identifier file provides additional exclusions for retained string values,
including GFIDs and AFR values. Typed prefixes are accepted for compatibility;
they no longer select replacement names. Any matching retained string is omitted
(case-insensitive substring match). Unknown free text is always omitted, even
without a matching private identifier. Keep the identifier file outside the
repository and run from the source checkout:

```bash
python3 -m gluster_heal_tool.support_bundle PRIVATE_DESTINATION \
  --private-identifiers PRIVATE_IDENTIFIER_FILE \
  --artifact heal_info=EXISTING_HEAL_INFO_FILE \
  --artifact observations=EXISTING_OBSERVATIONS_JSON \
  --artifact manifest=EXISTING_MANIFEST_JSON
```

Each exported file is JSON with `diagnostic_schema_version: 1`, its artifact
label, a `metadata` projection and an `omitted_fields_or_lines` count. The
exporter uses explicit container and value schemas; it never copies a raw
artifact after applying a text filter. These projections are diagnostic records,
not valid repair inputs or evidence of current execution authority.

| Artifact label | Accepted saved format and retained metadata |
| --- | --- |
| `heal_info` | Gluster brick sections, explicit connected/disconnected or unknown connection state, entry counts, aliased paths, canonical GFIDs and split-brain flags |
| `volume_info` | Recognized Gluster volume name/type/state and parsed brick roles with aliased host/path topology |
| `volume_status` | Parsed Gluster brick and self-heal-daemon online/PID state with aliased hosts and paths |
| `health` | Version-1 health report: topology aliases, selected check outcomes, free space/inode counts, heal-option states, snapshot flags and summary counts |
| `manifest` | Writer JSON with an `objects` map: identity, object type, observations, host roles and aliased origin topology |
| `observations` | Writer JSON with an `observations` list: GFIDs, existence/type checks, sizes, modes and ownership aliases |
| `plan` | Writer JSON with an `actions` list: defined action types, dependencies, source aliases, selected metadata and roles |
| `apply`, `execute_results` | Apply/execution writer JSON with an `actions` list: defined outcomes, step types, return codes, estimates and aliased paths |
| `status` | Status writer JSON: volume alias, write/unknown/interruption flags and selected action counts |
| `afr_inspection` | Direct inspector-result JSON with `path` and `afr_xattrs`: aliased xattr names, exactly 12-byte AFR counter values |

Messages, notes, command arguments, stdout/stderr, health log matches, snapshot
names, arbitrary xattrs, ACL text,
timestamps, source fingerprints and unknown fields are omitted. Unknown enum
values and values with unexpected types are omitted too. The omission count
counts excluded fields/values or heal lines, not bytes or all descendants of
an excluded container. Unsupported writer schema versions are not exported.

`brick_roles`, `resolver_record`, `decisions`, `summary`, and `assistants` are
recognized labels without a schema exporter; their inventory status is
`omitted_unsupported_format`, with no raw copy. Unknown labels, including logs
and worker wrappers, are rejected before any artifact is read. Missing files
under recognized labels appear as `missing`. Exported files appear as
`exported_metadata` with their JSON filename and SHA-256 hash of the exported
bytes. Inspect the inventory before assuming that a selected artifact supplied
evidence.

The builder refuses explicit file-content fields, symlinks, nonregular files
(including FIFOs), non-UTF-8/NUL input, and artifacts larger than 8 MiB before
creating the output. It reads only the explicitly supplied saved artifact files.
The output directory is private (`0700`) and its files are private (`0600`).
It does not read server files referenced by artifacts, contact a host, or send
anything. There is no raw-copy fallback.

Inspect **every** exported file and `maintainer-handoff.txt` before sharing.
GFIDs, sizes, modes, counters and relationships remain diagnostic information;
pseudonymization is not a guarantee of anonymity or truthful input. Never pass
a payload dump as an artifact. Keep the source, identifier list and bundle under
the private operations area, outside the public repository.

The [diagnostic collection qualification checklist](DIAGNOSTIC_COLLECTION_QUALIFICATION.md)
maps each supported label to its bounded saved source and the remaining
disposable-fixture evidence. It does not authorize a repair or a submission.

For a lab control, first identify whether a disposable independent volume or
isolated host set can reproduce the identity and heal-row behavior. Document
topology, roles, expected rollback and the read-only baseline before any
write. Absence of a cohort-isolation helper establishes only that no such
helper is currently present; it does not establish that isolation is
impossible or requires a shared-host partition. Do not rerun an unresolved
repair to obtain this control.
