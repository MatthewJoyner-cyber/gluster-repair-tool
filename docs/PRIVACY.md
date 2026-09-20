# Public content and private state

The repair tool and agent companion are independent public source trees.
Each has a public TODO and current documentation. The core's HISTORY.md is a frozen, sanitized account of development before migration. New implementation history is recorded in detailed commits. Neither needs personal files
to build, run offline tests, or show help.

| Location | Content |
| --- | --- |
| Repository `steering/TODO.md` | Product bugs and release tasks |
| Repository `HISTORY.md` | Portable design decisions, summarized history, caveats |
| Git commit messages | Future implementation details, rationale, validation, caveats |
| `$HOME/.gluster-repair-private/steering/` | Personal preferences, checkout mappings, host inventory, local helper configuration |
| `$HOME/.gluster-repair-private/ledger/` | Personal TODO, live incidents, run identifiers, evidence and support history |

`GLUSTER_REPAIR_PRIVATE_ROOT` may select a different private root for agent
guidance. It is a documentation/agent convention, not a core runtime setting.
Keep it outside every repository and export directory. Read only the relevant
private entry for a local task; do not ingest the whole archive by default.
Absent private files are normal for a new user.

Private steering may supply local paths and preferences. It cannot change the
repair engine's invariants or turn an old incident record into fresh authority.
Do not place credentials in Markdown ledgers; refer to the user's credential
manager or SSH configuration without copying secrets.

For a public change, restate only the transferable conclusion. Replace incident
examples with constructed fixtures; do not rename a real transcript and claim
it is a reproducible public test. Names, addresses, usernames, private paths,
GFIDs linked to incidents, host topology, timestamps in raw logs, customer
filenames, and screenshots all require review. Preserve required licensing and
public source attribution; removing private context does not remove obligations.

Use separate private logs while working. The interactive handoff source can
contain raw names and paths; do not share it. A maintainer diagnostic bundle
uses pseudonyms and private output permissions, but every copied file still
needs human review before a user chooses to share it. The tool sends nothing.

The original repository and its history remain private reference material.
A new tree is not a sanitized history rewrite. Before public initialization,
inspect the exact file list, generated packages, comments, examples, tests,
symlinks, and metadata. Before the first public commit, deliberately select a
public Git author identity; inspect the initial commit as well as its files.

Run the companion's `scripts/audit_tree.py` on each candidate, optionally with
a private literal-pattern file stored outside the trees. The scan reads all
text, including comments and ledgers; it reports locations without printing
matched values. A clean scan is a check, not a guarantee against unknown PII.

## Chosen public maintainer information

MAINTAINERS.md may contain a person's chosen alias/name and explicitly supplied
public contact details. This exception is limited to the fields they elect to
publish. It does not permit disclosure of unrelated personal information.
Check author metadata at commit time as well as file contents.
