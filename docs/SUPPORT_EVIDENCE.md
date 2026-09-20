# Maintainer diagnostic evidence

After publication, users may choose to send a diagnostic case to the Gluster
Repair Tool maintainers. The collection tool prepares a local bundle; it never
sends one. The intended evidence is metadata such as object names, sizes,
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

Typed values become stable aliases (`server1`, `organization1`, `path1`) across
the bundle. `secret:` values and untyped legacy entries become `[REDACTED]`.
Structured Gluster brick host fields and real IP addresses are also assigned
stable `serverN` and `ipN` aliases automatically. List organization names,
personal names, account names, sensitive path roots, and any hostnames that
appear only in unstructured text; the tool cannot infer all of them. Keep the
identifier file outside the repository and run from the source checkout:

```bash
python3 -m gluster_heal_tool.support_bundle PRIVATE_DESTINATION \
  --private-identifiers PRIVATE_IDENTIFIER_FILE \
  --artifact heal_info=EXISTING_HEAL_INFO_FILE \
  --artifact afr_inspection=EXISTING_AFR_OUTPUT_FILE \
  --artifact resolver_record=EXISTING_RESOLVER_RECORD_FILE
```

Missing paths appear only as `missing` in `inventory.json`. Present UTF-8
metadata files are copied with aliases applied and SHA-256 hashes of the
anonymized copies recorded. The builder refuses obvious file-content fields,
unlisted credentials and URLs, symlinks, binary text, and oversized artifacts.
Email addresses and user-home prefixes are removed. The output directory is
private (`0700`) and its files are private (`0600`). It does not read server
files referenced by artifacts, contact a host, or transmit anything.

The resulting `maintainer-handoff.txt` is a starting point. Inspect **every**
copied file before sharing: arbitrary diagnostic text can still contain an
organization, person, path or secret that no automatic check recognizes.
Never pass a payload dump as an artifact. Keep the source, identifier list and
bundle under the private operations area, outside the public repository.

For a lab control, first identify whether a disposable independent volume or
isolated host set can reproduce the identity and heal-row behavior. Document
topology, roles, expected rollback and the read-only baseline before any
write. Absence of a cohort-isolation helper establishes only that no such
helper is currently present; it does not establish that isolation is
impossible or requires a shared-host partition. Do not rerun an unresolved
repair to obtain this control.
