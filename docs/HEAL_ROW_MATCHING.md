# Canary heal-row matching

Canary capture interprets `gluster volume heal ... info` as structured rows.
It derives the expected volume-relative path from the canary's recorded mount
root and mount file, then compares complete parsed entries. A matching row is
exactly `/scenario/.../object`, or an exact canonical `<gfid:UUID>` when that
GFID was recorded for the object. The parser also accepts Gluster's
` - Is in split-brain` row suffix.

A shared basename, pathname substring, neighbouring path prefix, similar
mount root, or path text in a header is not evidence for the canary. Invalid
mount identities cannot match. The matcher is used by both file metadata
split-brain canary creation paths when they record before/after visibility.

This fixes saved canary visibility classification, not the availability or
freshness of Gluster heal output. A saved match does not authorize a repair;
the plan and execution gates still need current independent evidence. The
POSIX source-choice bridge separately checks its saved snapshots and fixture
provenance as described in [source-choice scope](CANARY_SOURCE_CHOICE.md).
