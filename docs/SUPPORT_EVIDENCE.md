# Support evidence and local draft

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
operation is absent. A support draft should also include existing resolver
records, heal output, volume info/status, role evidence and relevant run
artifacts. If an artifact was never collected, list it as missing; do not
repeat a destructive or unresolved repair merely to fill a bundle.

`simple` mode's handoff lists paths as **present** or **missing** and calls
itself a local draft. To make a copied, redacted text bundle from already saved
artifacts, create a private file with one literal private identifier per line
(hostnames, account names, local paths, addresses, and other known identifiers),
then run from the source checkout:

```bash
python3 -m gluster_heal_tool.support_bundle PRIVATE_DESTINATION \
  --private-identifiers PRIVATE_IDENTIFIER_FILE \
  --artifact heal_info=EXISTING_HEAL_INFO_FILE \
  --artifact afr_inspection=EXISTING_AFR_OUTPUT_FILE \
  --artifact resolver_record=EXISTING_RESOLVER_RECORD_FILE
```

Missing paths appear only as `missing` in `inventory.json`. Present UTF-8
files are copied with supplied identifiers redacted and SHA-256 hashes
recorded. The builder rejects unlisted email and home-directory identifiers,
symlinks, binary text, and oversized artifacts. It does not collect from a
host, inspect payload bytes, or submit anything. The resulting
`submission-draft.txt` is a starting point: review every copied file, including
any other private identifiers the automated check cannot know, before sharing.
Keep the source and output under the private operations area, outside the
public repository. No support case submission is claimed by this project.

For a lab control, first identify whether a disposable independent volume or
isolated host set can reproduce the identity and heal-row behavior. Document
topology, roles, expected rollback and the read-only baseline before any
write. Absence of a cohort-isolation helper establishes only that no such
helper is currently present; it does not establish that isolation is
impossible or requires a shared-host partition. Do not rerun an unresolved
repair to obtain this control.
