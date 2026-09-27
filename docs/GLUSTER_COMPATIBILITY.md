# Gluster version and output compatibility

The tool was developed on Gluster 10.x. It is intended to work with Gluster 10
and newer releases when their relevant interfaces behave as expected, but each
version and feature still needs live qualification. The current controlled VM
baseline is Ubuntu 24.04 with Gluster 11.1. **No compatibility claim is made
for versions before Gluster 10**: older CLI, heal output, or resolver behavior
may differ. A precheck displays a warning for a recognized version below 10;
the warning does not block evidence collection.

The controller records the installed `gluster --version` value for capability
reporting. An exact version selects only features proved on that release in a
disposable live lab. A numeric version by itself no longer makes every command
available. The compatibility profile is in
[`gluster_compat.py`](../gluster_heal_tool/gluster_compat.py).

The [first-beta plan](FIRST_BETA_RELEASE_PLAN.md) keeps the current gates and
defers enabling unqualified optional commands. Compatibility review prioritizes
changed/deprecated interfaces, output and helper contracts, and behavior changes
that invalidate a safety assumption. A Gluster bug fix that heals a former
canary normally needs a scoped observation, not another forced repair case.
Untested versions are unqualified, not automatically known to be incompatible.
Minor updates within a major Gluster line are expected to preserve ordinary
interfaces, but the tool's safety-sensitive native command gates still require
an explicitly qualified version. The major-version boundary matters most for
broader compatibility review; a minor release can still change an observed
response or fix a fault that once required intervention.

The 11.1 lab also completed existing operator-seeded ghost-handle and directory
child-gap repair cycles, including an independently verified directory `mdata`
follow-up. See [validation](VALIDATION.md). These were constructed faults with
healing held off, not evidence that 11.1 has either naturally occurring defect.

The [11.1 release notes](https://docs.gluster.org/en/main/release-notes/11.1/)
list a volume-type display fix, but the linked
[replica-2 report](https://github.com/gluster/glusterfs/issues/4107) still shows
`Type: Distributed-Replicate` with `Number of Bricks: 1 x 2 = 2`. The lab's
replica-3 volume reports `Replicate`, so it cannot validate that replica-2
case. The tool trusts the reported type and refuses repair discovery when it
is not exactly `Replicate`; it does not reinterpret a possibly mislabeled
volume from its brick count. Replica-2 support under that output is therefore
unqualified. Gluster 11.0 also lists a
[replicated virtual-image healing issue](https://docs.gluster.org/en/main/release-notes/11.0/).
No matching shipped canary was found, so no native-heal conclusion is drawn
for that workload.

| Feature | Gluster 11.1 qualification | Other versions |
| --- | --- | --- |
| Pending index heal | Scoped live canaries passed | Unqualified |
| Full namespace heal | Quiet-volume smoke and a pending-row command observation passed; concurrent native healing leaves command causality unproved, so tool launch remains blocked | Unqualified; launch blocked |
| Native per-file split-brain resolver | Response parser has synthetic tests; no live write proof, so execution is blocked before dispatch | Unqualified; execution blocked |
| Heal-info connection state | Connected sections and an offline section lacking `Status` were observed; only explicit connected states can establish a complete post-repair check | Require the same recognized output shape; other releases still need live qualification |

The post-repair completion check requires every brick section to state
`Status: Connected` and a numeric entry count matching the parsed rows. A
missing status, disconnected brick, nonnumeric count, unfamiliar line or
inconsistent count makes the check unavailable. The CLI then reports an
incomplete run and retains backup artifacts. This is a response-format gate:
it does not infer connectivity from the Gluster version.

The shared pending-index and full-namespace heal functions also check the
installed version immediately before issuing a command. This covers manager,
repair and development-canary callers that bypass the capability report. A
developer qualifying a new command format must use a controlled disposable
lab procedure before changing the profile; ordinary tool entry points do not
override an unqualified feature.

The metadata-only diagnostic exporter keeps the brick's connection state as
`unknown` when `Status` is missing. It can still export other recognized
metadata for human review. Preview and evidence collection remain available
for unqualified versions. An unknown native resolver format cannot authorize a
resolver write, and an unknown heal-info state cannot establish completion.

To qualify another version or feature, record the exact installed version and
command form privately, capture connected/offline and success/failure outputs
on disposable volumes, add parser fixtures for unfamiliar shapes, and repeat
the bounded live command test. Only then add the exact version/feature pair to
the profile. The Gluster 11.1 lab accepted full-heal commands on both a quiet
volume and one with a pending row. In the latter run, all copies converged,
but an active self-heal daemon may have performed the data heal independently.
This does not qualify a repair effect attributable to the full command. The
lab has not exercised a native split-brain resolver write path.
