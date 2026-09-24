# Verified backup archives and restoration

New archives use manifest version 3. Their fidelity inventory records selected
file contents, numeric ownership, full permission bits, nanosecond modification
times, symlink targets, extended attributes and hardlink groups. Directory
metadata is restored after its children. Unsupported objects and inaccessible
metadata stop capture rather than producing a silently incomplete archive.

Remote capture and restoration transfer all selected artifacts for one host
together, preserving hardlinks across separate entries. A NUL-delimited file list
limits the transfer to the recorded absolute paths. Host namespaces remain
separate; no hardlink may cross them. Transfers preserve hardlinks, POSIX ACLs,
xattrs and numeric IDs. On the controller, rsync's fake-super representation
stores privileged ownership and attributes without needing controller root.
The encoded attributes are themselves preserved and verified in the archive.
The remote helper must have the required privileges to read or restore them.
See the upstream [rsync manual](https://download.samba.org/pub/rsync/rsync.1) for
fake-super, ACL/xattr and file-list semantics.

Capture performs a checksum and metadata comparison against the remote source.
Archive creation checks every member's data, metadata and hardlink relationships,
then checks that the staged sources did not change while being archived. A new
archive replaces an existing file only after verification and fsync. A failed
replacement leaves the earlier archive intact.

Cleanup requires that archive explicitly. It verifies archive contents, restores
them into disposable staging, and checks current source backups against the
capture before removing anything. Missing, changed, unreadable or unverifiable
backups stop cleanup. `--cleanup-backups` without an archive no longer discards
the only recovery copy; combine it with `--archive-backups PATH`.

Restore first validates the archive and its staged metadata. Local destinations
are checked after restoration. Remote restoration uses the same complete host
selection and then a read-only checksum/metadata comparison. A zero transfer
exit alone does not authorize archive removal. Permission failures, transfer
loss, verification differences and skipped conflicts retain the archive.
Legacy archives remain readable, but cannot authorize backup cleanup or automatic
archive deletion because they lack the fidelity inventory. Missing metadata in
an old archive cannot be reconstructed by adding newer transfer flags.

Remote restore verification reads back through the same fake-super encoding
used for capture. A push-direction dry run can report xattr differences between
encoded and real privileged attributes even after correct restoration. The
read-back comparison uses deletion reporting only with `--dry-run`, so a child
missing remotely is detected without deleting either staged or remote data.

The contract covers the selected backup artifacts, including hardlinks between
them. It does not prove that an earlier repair-step backup captured the original
live object correctly, or recreate links to paths outside the selection. Overlapping
artifact mappings are rejected rather than guessed. Keep backup trees quiescent:
verification and deletion are separate operations, not a distributed snapshot or
transaction. The inventory is an integrity check, not a trusted signature.

Local Linux tests use actual files and rsync 3.2.7 client/server processes over
local pipes, with separate synthetic host roots. They cover archive, verified
cleanup, restore, xattrs, modes, timestamps, symlinks, host identity and hardlinks
across artifacts. Fault tests cover inaccessible metadata, changed/corrupt
backups, failed verification and restoration permissions. A synthetic fake-super
fixture verifies that encoded privileged metadata survives archival; it is not
privileged trusted-xattr proof. Disposable privileged testing of trusted xattrs,
numeric ownership changes and ACL behavior, plus deployed-host/version
qualification beyond the scoped VM result below remains required before release.

On 2026-09-24, a disposable Ubuntu 24.04 / Gluster 11.1 VM lab exercised the
installed helpers over real SSH from an unprivileged controller. Recovery from
a retained archive passed independent comparisons on two remote guests for
numeric ownership, access/default ACLs, trusted/user xattrs, nanosecond mtime,
symlinks, and hardlinks spanning artifact roots. This exposed and motivated the
read-back verification fix above. This scoped VM result does not establish
cross-distribution coverage or the fidelity of backups made by earlier repair
steps. A subsequent fresh archive/verified-cleanup/restore cycle also passed
the independent comparisons on both guests. The full local suite passed 834
tests, including real-rsync checks that read-back detects missing children and
changed xattrs while leaving both comparison trees untouched.
