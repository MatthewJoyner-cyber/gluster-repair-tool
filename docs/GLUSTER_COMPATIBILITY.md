# Gluster version and output compatibility

The controller records the installed `gluster --version` value for capability
reporting. An exact version selects only features proved on that release in a
disposable live lab. A numeric version by itself no longer makes every command
available. The compatibility profile is in
[`gluster_compat.py`](../gluster_heal_tool/gluster_compat.py).

| Feature | Gluster 11.1 qualification | Other versions |
| --- | --- | --- |
| Pending index heal | Scoped live canaries passed | Unqualified |
| Full namespace heal | No live command proof; launch blocked | Unqualified; launch blocked |
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
the profile. The currently retained Gluster 11.1 lab did not exercise the full
namespace or native split-brain resolver write paths.
