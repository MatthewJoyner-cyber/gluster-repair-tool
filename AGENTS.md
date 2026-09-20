# Repository agent rules

Read steering/SESSION_STARTER.md and steering/TODO.md for continuation.
Preserve unrelated changes and commit only when requested.

Keep personal settings and live evidence outside the repository according to
docs/PRIVACY.md. Public comments, tests, commit messages, and examples must be portable.
Use synthetic fixtures; never copy a private transcript into a public issue.

Prefer an available bounded helper for the operation it covers. Do not widen
permissions or substitute an unrestricted command to evade a blocked helper.
Keep edits readable and independently privileged steps separately reviewable.
The optional companion supplies agent-specific procedures; the core needs none.

Start with evidence and preview. Follow steering/SAFETY_INVARIANTS.md. Never
use an arbiter as a payload source, treat canary setup as independent evidence,
or retry a possibly completed write without focused verification.

Run meaningful focused tests after code changes and the complete suite for
shared behavior. Record synthetic validation separately from live proof.
Each commit records the problem, behavior, rationale, validation, and caveats.
Do not import or continue the old ledger. After review remediation is implemented
and validated, record the result,
commit when authorized, and create a distinct annotated review-YYYY-MM-DD tag.
For subsequent reviews, inspect the newest review tag and state the range.
This new history has no inherited review tag; the private predecessor's tags
are not release acceptance.
