# Development workflow

Use current artifacts for evidence, logical objects for planning, and a
mechanical executor for approved actions. A possible write with an unknown
result requires verification and new planning before another attempt.
See [SAFETY_INVARIANTS](SAFETY_INVARIANTS.md).

Use small modules with clear ownership; see [architecture](../docs/ARCHITECTURE.md).
Add a failing regression for a demonstrated defect, fix it, and run the focused
suite. Run the complete suite after shared changes. Do not describe a mock as
live repair proof.

Record future tasks in TODO. Record implementation details in each commit:
problem, resulting behavior, rationale, validation, and remaining caveats.
HISTORY.md is the frozen pre-migration summary; do not continue the old ledger. Personal deployment facts and raw incident history go
to the external private ledger. Public notes may preserve the lesson and the
limits of the evidence, without inventory or identifying artifacts.

When reviewing recent work, locate the newest completed review tag and record
the reviewed range. Create a new annotated review-YYYY-MM-DD tag only after
all scoped remediation is implemented, validated, and committed with authority.
Do not move an existing tag or imply an unimplemented review was completed.

At migration, initialize independent histories for the core and companion.
Keep the old repository private for reference only. Public summaries replace
raw imported history; no private checkout is a build or runtime dependency.
