# Host-helper command contract

The controller generates SSH commands that invoke `gluster-host-ops.sh` through
the service account's sudo policy. Deploy the controller and helper from the
same candidate version. An older helper rejects directory mdata writes and
does not implement the generated `stat` query.

The helper now accepts these attribute-write forms with exactly one target:

| Attribute | Accepted value |
| --- | --- |
| `trusted.gfid` | `0s` followed by canonical base64 encoding of exactly 16 bytes |
| `trusted.glusterfs.mdata` | `0x` followed by one or more complete hexadecimal bytes |
| `trusted.afr.<name>` | The same hex encoding; a nonempty name containing letters, digits, dots, underscores or hyphens |

Malformed encodings, extra operands, other attribute namespaces and attribute
removal requests fail before invoking `setfattr`. Encoding checks do not
establish source authority or validate a Gluster-version-specific mdata/AFR
payload structure. Repair eligibility and fresh evidence remain separate gates.

Generated directory creation uses `mkdir -p -- PATH`, matching the helper and
executor. The helper's new stat operation accepts only `stat -c %F -- PATH`.
Paths containing spaces or shell punctuation remain individual quoted operands
for these commands. GFID-link operations retain their stricter path restrictions.

ACL transfer previews use a non-login Bash with `pipefail`, so a failed source
`getfacl` cannot be hidden by a successful target `setfacl`. Failure clearing the
default ACL stops the transfer. This is failure reporting, not transactional
rollback: clearing or partially transferring ACLs can already have changed the
target before a later failure. Inspect retained attempt records and current
metadata before retrying.

## Local qualification

The contract suite sends actual generated SSH argv into the actual shell helper
while replacing SSH/sudo transport and low-level operations with local stubs.
It covers mdata preview/executor agreement, GFIDs, AFR encoding, metadata
owner/mode/ACL commands, directory creation, stat/getfattr, copy/move/removal
commands, planned residue backup/revert commands, GFID links, and brick-side
rsync/client-server dispatch. Cases include quoted paths, invalid encodings,
extra operands and propagated command failures.

These tests qualify command compatibility and refusal behavior. Separate
Ubuntu 24.04 VM checks exercised deployed helpers/sudoers and backup round trips
with trusted/user xattrs, ACLs and numeric ownership; see
[backup fidelity](BACKUP_FIDELITY.md). This does not qualify every repair metadata
write or other platform/version combinations. Rsync server
dispatch remains a privileged interface; this change does not introduce a new
rsync option or filesystem-path confinement policy.
