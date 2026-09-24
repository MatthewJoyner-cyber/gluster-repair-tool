# Canary cases and public references

This is a sanitized reference map, not an operational ledger. Public reports
motivate a test shape; they do not prove the same defect exists in the tested
Gluster version or authorize a workaround. Links were checked on 2026-09-24.
Do not copy reporters' names, contacts, organizations, paths or raw logs into
fixtures. Use synthetic content and identities.

## Public cases

| Source | Reported or documented shape | Test implication and limit |
| --- | --- | --- |
| [Gluster-users: not healing one file, October 2017](https://lists.gluster.org/pipermail/gluster-users/2017-October/032777.html) | An older replica-3 installation had one disagreeing GFID and a client I/O error; the thread reports recovery after a manual intervention and discusses newer resolver support. | Compare logical path, GFID and handle relationships across replicas. Use current-version discovery/resolver behavior; do not transplant the thread's deletion workaround. This is historical shape evidence, not a reproduced current defect. |
| [Gluster-users: FUSE visibility after parent listing, April 2018](https://lists.gluster.org/pipermail/gluster-users/2018-April/033918.html) | A report describes missing client-visible entries becoming visible after a parent directory listing; a reply requests further metadata to investigate. | Record client and brick views before and after a lookup. The source proposes hypotheses, not a settled diagnosis. A canary self-healing on lookup is native-heal evidence, not proof that the repair engine fixed it. |
| [Official split-brain troubleshooting](https://docs.gluster.org/en/main/Troubleshooting/resolving-splitbrain/) | Distinguishes data, metadata, GFID and type conflicts, with resolver-specific restrictions. | Keep source authority and conflict families separate. A timestamp or size is not independent evidence of the intended content. Confirm the installed version's supported resolver and handle type conflicts conservatively. |

## Local qualification families

| Family | Independent observations | Current proof scope |
| --- | --- | --- |
| Replica-3 native pending metadata | Fresh heal/brick state and installed repair preview after shipped fixture construction | Completed with native healing to zero pending entries and no proposed repair writes; smoke/native-heal-first. |
| Replica-3 access ACL mismatch kept online | Fresh heal state, roles and installed preview, separate from builder state | Native healing cleared the tested mismatch; no retained-fault repair-apply claim. |
| Replica-3 directory default ACL | Fresh heal state and installed preview after the directory builder | Setup/discovery and native-heal-first evidence; not general directory reconciliation acceptance. |
| Privileged backup recovery | Independent ownership, mode, timestamp, ACL, user/trusted xattr, symlink and cross-artifact hardlink comparisons | Recovery from a retained failed archive and a fresh round trip passed on the tested Ubuntu guests. This is backup proof, not split-brain repair proof. |
| Saved execution identity | Actual volume recreation at identical endpoints; separate fresh worker observations before/after a same-size file change with restored modification time | Installed CLI refused the obsolete volume generation and changed backend evidence before dispatch. Unchanged observations passed. These are admission/refusal proofs, not repair acceptance. |
| Stable missing file replica | Shipped builder with healing held off; independent backend discovery found two matching copies and one missing copy, then the reviewed public apply pipeline ran | One restore action completed; independent checks found the same GFID, size, mode, ownership and empty-content digest on all three bricks. Operator-path mechanical repair proof for an empty synthetic file; no payload-fidelity or protocol split-brain claim. Healing was restored afterwards. |
| Sequential guest restart | Boot identity, persistent name mappings, every guest's management peers, real service access and quiet connected heal state before the next restart | A first pass missed lost hostname mappings despite quiet heal output. After correcting the lab's persistent mapping, the strengthened three-guest rerun passed. Quiet heal output alone is insufficient; wider outage/platform coverage remains separate. |

See [validation](VALIDATION.md), [backup fidelity](BACKUP_FIDELITY.md),
[test plan](../steering/TEST_PLAN.md) and [current TODO](../steering/TODO.md).
Raw commands, manifests, failed attempts and run-to-source mappings remain in
the external private ledger. The public history retains only transferable
findings and the limits of the evidence.

## Adding a case

Record source title/date/link, reported version and symptom, whether the result
is a report, hypothesis, confirmed upstream fix or local reproduction, and the
specific missing evidence. For a local test, retain the exact candidate revision,
independent pre/post observations and proof label privately; publish only a
sanitized conclusion and synthetic reproduction. Never label a support draft
submitted unless it was actually sent with authority.
