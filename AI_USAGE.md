# AI usage

## Tools

| Tool | Used for |
|---|---|
| **Claude Code** (VS Code extension, Claude Opus 5.5) | Almost all code, tests and README text, written in one long session driven by my prompts. It also ran the commands: the generator, pytest, uvicorn, curl smoke tests, and clean-virtualenv installs. |

No other AI tools (Codex, Cursor, Copilot) were used for this project. I owned the requirements,
reviewed the output, made the commits, and pushed back where the result didn't meet the brief.

## Key prompts

1. **Task 1.** The full assignment brief, pasted verbatim: seeded AML ledger, provider interface
   with FakeProvider, sqlglot validator, read-only executor with row cap and 2 s timeout,
   `POST /ask`, and tests.
2. **"How can I test it?"** Asked for exact run and test commands, including Windows/PowerShell
   variants.
3. **Task 2.** *"Act as a senior Python security engineer… First inspect the entire existing
   project… Do not rewrite working Task 1 functionality… Regex-based SQL security checks are
   not acceptable… Write tests for every security requirement… Be honest about anything not
   fully implemented."* It specified roles from YAML, column enforcement on the AST, masking
   based on column lineage, a role-filtered prompt schema, a separate append-only audit DB, and
   a list of 12+ bypass tests.
4. **Task 2 review.** *"Compare the PAN masking implementation against the assignment's
   explicit example ABCDE1234F → XXXXXX1234… Review whether every POST /ask request, including
   malformed requests and missing fields, can be audited… missing or unknown roles must return
   HTTP 403."*
5. **Task 3.** *"Act as a senior AI evaluation engineer… Execute each question through the real
   /ask processing pipeline… Do not bypass the security controls… FakeProvider score must be
   below 100%… Do not claim completion without actual test output."*

Two habits made the biggest difference:
- asking the AI to **inspect before changing**;
- asking it to **report limitations honestly** instead of claiming guarantees.

## Where the AI was wrong, and how it was caught

### Caught by me, during review

| What the AI did | How it was caught | Fix |
|---|---|---|
| Masked PAN as `XXXXXX234F` (last 4 *characters*). It noticed this conflicted with the brief's example `XXXXXX1234` but just documented the deviation instead of meeting the acceptance criterion | My review prompt (#4) | Changed to "last 4 **digits**". Output is identical for phone and account numbers, and the brief's example is pinned in a test |
| Listed "malformed requests return 422 and aren't audited" as a *known limitation*. Worse, a missing role plus a bad body returned **422 instead of 403**, which violates the requirement | My review prompt (#4). The AI confirmed it with a probe script before fixing | A validation-error handler scoped to `/ask`: role checked first (403), otherwise the standard 422; exactly one audit record either way |

### Caught by tests or the AI's own verification

| What went wrong | How it was caught |
|---|---|
| The validator let `SELECT sqlite_version()` through: sqlglot renames it to a generic `CurrentVersion` node, so the function blocklist missed it | Table-driven validator test failed. Fix: also check the function name as rendered for SQLite |
| A scripted find-and-replace silently didn't apply (escaping mismatch). Every `/ask` call then returned `"internal error"` | 27 tests failed at once. The catch-all turned the crash into `status: error`, not a 500, which showed the fail-safe works |
| The auditor's prompt schema still said `customer_id … FK->customers`, naming a table the auditor can't see | Printing and reading the rendered role schemas |
| Policy resolution let a column entry re-open a table denied as a whole | AI self-review before writing tests; table-wide deny now always wins |
| Two code paths in the lineage analysis *skipped* columns they couldn't resolve, i.e. failed open | AI self-review; both now reject the query |
| Design flaw: "show last 4" on a *transformed* value leaks hidden characters, e.g. `'XXXX' \|\| substr(pan,1,4)` puts the PAN's first 4 characters in the visible tail | Reasoning about the bypass list; transformed values are now fully masked |
| A demo fixture (`account A00001`) and a "narration mentions drop" query returned 0 rows, so the demos looked broken | End-to-end smoke run; switched to an account with data |
| An eval test asserted `p95 >= mean * 0`, which is always true | AI re-read the test before running; replaced with a real bound |
| A Task 2 test chose "pattern questions" by `startswith("Which")`. Adding the Task 3 eval fixtures broke it | Full-suite run after Task 3 |
| Windows console crashed printing `₹` (cp1252) during probe scripts | Probe output; runner now uses `stdout.reconfigure(errors="replace")` |

## How AI output was verified (not just trusted)

- **Mutation testing**: each security mechanism was deliberately disabled to make sure the
  tests fail, so a green suite means something.
  - Task 2: disabling masking failed 37 tests; skipping the deny check failed 46; ignoring
    UNION lineage failed 4.
  - Task 3: comparing as sets instead of multisets, ignoring ORDER BY, comparing raw SQL
    strings, and a spy that never flags anything each broke 1–5 tests.
- **Leak checks against real data**: masking tests load every real PAN, phone and account
  number and assert none appears in any returned cell, rather than trusting `masked_columns`.
- **A clean virtualenv** install plus a full `pytest` run after every task, with no API keys.
- **A live-server smoke test** (uvicorn plus curl) after Tasks 1 and 2.
- **A "metric isn't vacuous" test**: disable the access policy and check the
  unsafe-execution counter actually fires.

## What still needs human judgement

- **Roles are self-asserted** in the request body. Real authentication is out of scope and
  documented.
- **Inference through filters**: masked columns are allowed in `WHERE`, as the brief requires,
  so a PAN can be recovered one character at a time. This is documented, not fixed.
- **The eval dataset and the deliberately wrong answers were written by the same AI** that built
  the pipeline. An independent, analyst-written question set would be a stronger test.
