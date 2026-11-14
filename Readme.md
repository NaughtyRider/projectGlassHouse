# GlassHouse: guarded text-to-SQL for AML investigations

GlassHouse turns an analyst's question into SQL, checks that the SQL is safe and read-only,
runs it against a read-only copy of a synthetic AML ledger, and returns the rows.

```
role check ─► role-filtered ─► LLMProvider ─► SQL validator ─► access policy ─► guarded executor ─► lineage
(403 if        schema prompt    (structured    (1 SELECT,       (column deny,    (read-only, authorizer   masking
 unknown)                        output)        table allow-     star reject,     incl. denied columns,      │
                                                list)            lineage plan)    row cap, 2 s deadline)     ▼
                                     every request ──────────────────────────────────────────────► audit DB (separate)
```

Status:
- **Task 1** (safe text-to-SQL vertical slice) is done.
- **Task 2** (role-based column access, PII masking with lineage, audit) is done.
  See [Task 2](#task-2-role-based-access-pii-masking-and-audit).
- Task 3 (evaluation) is not started.

## Setup and run

Requires Python 3.11+. No API keys or network access are needed.

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

python -m glasshouse.generate --seed 42      # builds data/glasshouse.db (~1 s)
pytest -q                                     # 323 tests, offline, ~20 s
uvicorn glasshouse.app:app --port 8000        # serves the API (run from the repo root)
```

```bash
curl -s -X POST localhost:8000/ask -H 'content-type: application/json' \
  -d '{"question": "Which accounts sent or received SWIFT wires involving high-risk countries?", "role": "l1_analyst"}'
```

### Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `GLASSHOUSE_DB` | `data/glasshouse.db` | SQLite database path (used by both the generator and the API) |
| `GLASSHOUSE_PROVIDER` | `fake` | `fake` (deterministic fixtures) or `ollama` (optional local model) |
| `GLASSHOUSE_FIXTURES` | packaged `fixtures/fake_responses.json` | FakeProvider fixture file |
| `GLASSHOUSE_POLICY` | `config/access_policy.yaml` | Role/column access policy (validated at startup) |
| `GLASSHOUSE_AUDIT_DB` | `data/audit.db` | Separate audit database. Startup fails if it is the ledger file |
| `GLASSHOUSE_HIGH_RISK_COUNTRIES` | `IR,KP,MM,SY,YE,AF` | ISO-3166 alpha-2 list used by the generator |
| `GLASSHOUSE_TIMEOUT_SECONDS` | `2.0` | Per-query wall-clock limit |
| `GLASSHOUSE_OLLAMA_URL` / `GLASSHOUSE_OLLAMA_MODEL` | `http://localhost:11434` / `llama3.1` | Only used when the provider is `ollama` |

## Reproducibility

The generator draws everything from a single `random.Random(seed)` and fixed calendar dates
(1 Jan 2026 + 180 days). It prints the row counts and a SHA-256 over every column of every
transaction, in `txn_id` order. Run it twice to compare:

```bash
python -m glasshouse.generate --seed 42 --db /tmp/a.db | tail -1
python -m glasshouse.generate --seed 42 --db /tmp/b.db | tail -1
# transactions sha256: 49bf353b6a670873a4ea9eabe09186b4e7a209687fa81134645949f4a87ab479  (both)
```

`tests/test_generate.py::test_same_seed_is_reproducible` asserts the same thing.

Seed 42 produces: 1,000 customers, 1,500 accounts, 50,179 transactions, 436 alerts.

## Synthetic data and planted AML patterns

The tables are `customers`, `accounts`, `transactions` and `alerts`, with the required columns.
See [src/glasshouse/schema.py](src/glasshouse/schema.py). All six channels
(CASH, NEFT, RTGS, IMPS, UPI, SWIFT) appear. The generator plants **12 accounts per pattern**,
with no account in more than one pattern. Ground truth is written to a `planted_patterns` table,
which is **not** on the query allow-list, so the assistant itself can't read it.

| Pattern | Definition | How it is planted |
|---|---|---|
| **Structuring** | 3+ CASH credits of ₹40,000–₹49,999 on one account within 7 days | 3–5 deposits of a random whole-rupee amount in the band, spread over at most 6 consecutive days |
| **Dormant reactivation** | no transactions for 90+ days, then credits totalling ≥ ₹5,00,000 within 7 days | a few normal transactions in days 0–30, then nothing for at least 100 days, then 2–4 RTGS/NEFT credits totalling ₹5.5 L–₹25 L over at most 4 days |
| **High-risk-country wires** | SWIFT transactions whose `counterparty_country` is in the configured list | 1–3 SWIFT transactions per account with a country taken from `GLASSHOUSE_HIGH_RISK_COUNTRIES` |

Background data is generated so that it **can't match a pattern by accident**:
- background CASH credits skip the ₹40,000–₹49,999 band;
- background SWIFT traffic only uses low-risk countries;
- dormant accounts get no background transactions.

As a result, each detection query returns exactly the planted set (tested).

Planted accounts for seed 42:

- Structuring: A00108, A00172, A00587, A00711, A00746, A00762, A00770, A00830, A00938, A00949, A01317, A01499
- Dormant reactivation: A00020, A00074, A00229, A00341, A00390, A00438, A00687, A00706, A00838, A01149, A01200, A01260
- High-risk wires: A00060, A00092, A00180, A00214, A00218, A00498, A00656, A01045, A01273, A01336, A01416, A01489

### Example questions (one per pattern)

These return exactly the planted accounts through `POST /ask` with FakeProvider. This is asserted
in `tests/test_api.py::test_readme_examples_return_planted_accounts`.

1. *"Which accounts have 3 or more cash deposits between ₹40,000 and ₹49,999 within 7 days?"*
   Self-joins CASH credits in the band within a `datetime(ts, '+7 days')` window and keeps
   windows with 3 or more deposits.
2. *"Which dormant accounts were reactivated with at least ₹5,00,000 in credits within 7 days?"*
   Uses `LAG()` to find gaps of 90+ days, then sums credits in the 7 days after the gap.
3. *"Which accounts sent or received SWIFT wires involving high-risk countries?"*
   Filters on `channel = 'SWIFT' AND counterparty_country IN (...)`.

Fixture lookup ignores case, extra whitespace and trailing `?.!`. The fixture SQL hard-codes
the default high-risk list. If you change `GLASSHOUSE_HIGH_RISK_COUNTRIES`, update the fixture too.

## API

`POST /ask` with `{"question": str, "role": "l1_analyst" | "l2_investigator" | "auditor"}` returns:

```json
{"request_id": "hex", "status": "ok|refused|rejected|error", "sql": "...",
 "columns": [...], "rows": [[...]], "masked_columns": [], "reason": null}
```

| status | when |
|---|---|
| `ok` | the query ran. `sql` is the SQL as executed (with the row cap applied) |
| `refused` | the provider declined (including FakeProvider's answer to unknown questions) |
| `rejected` | the validator or the access policy blocked the SQL, or the role is missing/unknown (HTTP 403). It never reached the database |
| `error` | malformed provider output, provider failure, execution failure, `timeout: ...`, or the audit log could not be written |

The handler never returns HTTP 500. Any unexpected exception becomes `status: error`.
A missing, empty, unknown or wrongly-typed `role` gets **HTTP 403** with the same response body,
`status: rejected`, and a reason listing the valid roles.

**The role is checked first**, before any other body validation. So a request whose role is
missing or unknown gets 403 even if the rest of the body is also broken: no question, invalid
JSON, a JSON array, form-encoded data, an empty body. Only a request with a **valid role** but an
otherwise invalid body (a missing, empty, over-long or wrongly-typed `question`) gets FastAPI's
standard 422. That 422 body is unchanged (`{"detail": [...]}`), with one extra `request_id`
field. Both 403 and 422 responses carry an `X-Request-ID` header that matches the audit record.
Validation on other endpoints (e.g. `GET /audit?limit=0`) keeps FastAPI's default behaviour and
is not audited.
`GET /health` reports the provider, the configured roles and whether the database exists.
`GET /audit?limit=N` is described [below](#audit-trail).

## Safety design

**Provider interface** ([provider.py](src/glasshouse/provider.py)). Every provider returns a
`SQLGeneration` Pydantic model `{sql, tables_used, refusal_reason}`:
- the model is strict, rejects extra fields, and requires exactly one of `sql` or `refusal_reason`;
- anything else raises `ProviderError`, which becomes `status: error`;
- the prompt includes the schema description, but not the ground-truth table.

`OllamaProvider` is optional: it is lazily imported and never used in tests.

**Validator** ([validator.py](src/glasshouse/validator.py)) works on the sqlglot AST, not on
strings. It:
1. rejects SQL that doesn't parse (this catches tricks like `SEL/**/ECT`);
2. requires exactly one statement;
3. requires the root to be a `SELECT` or a set operation (`UNION` / `INTERSECT` / `EXCEPT`);
4. walks every node and rejects any of these anywhere in the tree: INSERT, UPDATE, DELETE,
   CREATE, DROP, ALTER, PRAGMA, ATTACH, DETACH, transaction control, `SELECT INTO`, or a
   generic `Command` (the fallback for things like `REPLACE INTO` / `VACUUM`). This is what
   catches `WITH x AS (DELETE … RETURNING *) SELECT …`;
5. requires every table reference to be an unqualified name on the allow-list or a CTE. This
   blocks `sqlite_master`, `sqlite_schema`, `main.x`, attached databases, and table-valued
   functions like `pragma_table_info()`;
6. rejects dangerous functions: `load_extension`, `readfile`, `writefile`, `randomblob`, and
   anything starting with `sqlite_` or `pragma_`. Names are checked both as parsed and as
   rendered for SQLite, because sqlglot renames some functions.

Case and comments don't matter, and keywords inside string literals (`LIKE '%drop%'`) can't
cause false rejections.

**Executor** ([executor.py](src/glasshouse/executor.py)) adds layers that hold even if the
validator were bypassed:
- the connection is opened read-only (`file:…?mode=ro`) with `PRAGMA query_only=ON`;
- a SQLite authorizer allows only SELECT, READ and FUNCTION actions, and only reads of
  allow-listed tables in `main`;
- the row cap is applied by rewriting the AST's outermost `LIMIT`: 200 if absent, clamped to
  1000 if larger, and 1000 if it isn't an integer literal (e.g. `LIMIT -1` or a subquery).
  `fetchmany(1000)` is a final backstop;
- a progress handler aborts the statement after 2 s of wall-clock time. The cartesian self-join
  `SELECT COUNT(*) FROM transactions a, transactions b` returns `status: error, reason: timeout: …`
  in about 2.0 s. A separate test shows it is still running after 2.5 s without the guard.

## Task 2: role-based access, PII masking and audit

### Architecture

Each concern has its own module, and each runs on the **parsed AST**, never on SQL text:

| Module | Responsibility |
|---|---|
| [config/access_policy.yaml](config/access_policy.yaml) | The permission data: roles and a `allow` / `mask` / `deny` action per column |
| [policy.py](src/glasshouse/policy.py) | Loads and strictly validates the YAML (unknown tables/columns/actions/keys fail at startup), does exact role lookup, renders the role-filtered prompt schema |
| [access.py](src/glasshouse/access.py) | Column enforcement and output lineage on the sqlglot AST |
| [masking.py](src/glasshouse/masking.py) | Applies the mask plan to result values, by output position |
| [audit.py](src/glasshouse/audit.py) | Append-only audit log in its own SQLite file |
| [service.py](src/glasshouse/service.py) | Orchestrates the pipeline and writes exactly one audit record per request |

Defence in depth for denied columns:
1. `access.py` rejects the query before execution.
2. The SQLite authorizer in [executor.py](src/glasshouse/executor.py) also receives the role's
   denied `(table, column)` pairs, so SQLite itself refuses to read them. Tests bypass layer 1
   on purpose to prove layer 2 holds.

### Permission matrix

| Column | l1_analyst | l2_investigator | auditor |
|---|---|---|---|
| customers.full_name | **deny** | allow | **deny** |
| customers.pan | **deny** | mask | **deny** |
| customers.phone | **deny** | mask | **deny** |
| other customers.* columns | allow (default) | allow (default) | **deny** (whole table) |
| accounts.account_number | mask | mask | **deny** |
| transactions.counterparty_account | allow (default) | allow (default) | **deny** |
| transactions.narration | allow (default) | allow (default) | **deny** |
| everything else | allow (default) | allow (default) | allow (default) |

**Default behaviour:** `default_column_action: allow`. Any column a role does not list is
allowed. A rule is resolved in this order:
1. A table-wide `deny` under `tables:`. It wins over any column entry, and also blocks
   column-less access such as `SELECT COUNT(*) FROM customers`.
2. An explicit `columns:` entry.
3. The default.

Roles are matched exactly and case-sensitively. **There is no default role**: a missing, empty,
unknown or non-string role is a 403 before the provider is even called.

**`SELECT *` is rejected** for every role, including `t.*` and stars inside subqueries.
`COUNT(*)` is still fine. I chose rejection over expanding the star because it is simple to
reason about. Expansion is safe only if it uses exactly the same resolution as the deny check,
and a later schema change could silently widen it. The model is told to list columns
explicitly.

### How alias resolution works

`access.py` runs sqlglot's `qualify()` against the column catalog. The catalog is itself parsed
from the DDL in [schema.py](src/glasshouse/schema.py), so it can't drift from the database. After
qualification:
- every column reference has the form `<source_alias>.<column>`;
- `JOIN … USING` is expanded into explicit equalities;
- references to an ORDER BY alias are resolved;
- unknown or ambiguous columns raise an error, and the query is rejected.

Then `build_scope()` splits the query into scopes: root, CTEs, derived tables, subqueries in
WHERE/SELECT/HAVING, and each UNION branch. For every column in every scope, the alias is
looked up in that scope's sources, walking up to parent scopes for correlated subqueries. It
resolves to either:
- a **base table** (`c` → `customers`): then the policy is checked for `(customers, pan)`; or
- a **derived scope** (a CTE or subquery): its own columns are checked when that scope is visited.

So `SELECT zz.phone FROM customers AS zz`, `… JOIN customers c ON … c.pan …`, and a denied
column inside a CTE that the outer query never selects are all caught.

### How column lineage protects PII

Masking is decided by **where an output value comes from**, not by its name. For each output
position of the root query, `access.py` computes the set of base columns that flow into it,
following:
- aliases and nested functions (`upper(substr(pan, 1, 10))`);
- concatenation;
- CTE chains and derived tables, by looking up the named output in the child scope;
- scalar subqueries in the select list;
- **every UNION branch**, with lineage merged position by position, so one masked branch masks the
  whole column.

If any contributing column is `mask` for the role, that output position is masked:

| Output expression (l2_investigator) | Lineage | Result |
|---|---|---|
| `pan` | direct passthrough of `customers.pan` | `XXXXXX1234` (partial: last 4 digits visible) |
| `pan AS customer_identifier` | direct | `XXXXXX1234` |
| `t.v` from `(SELECT pan AS v …) t`, or via a CTE | direct (alias/scope hops only) | `XXXXXX1234` |
| `substr(pan, 1, 10)` | transformed | `XXXXXXXX` (full) |
| `upper(pan)` | transformed | `XXXXXXXX` |
| `'PAN:' \|\| pan` | transformed | `XXXXXXXX` |
| `SELECT city … UNION SELECT pan …` | branch 2 is masked | whole column masked |
| `GROUP_CONCAT(pan)`, `CASE WHEN … THEN pan END` | transformed | `XXXXXXXX` |
| `account_number` (l1 / l2) | direct | `XXXXXXXXXXXX0012` |

**Why transformed values are fully masked.** Showing a tail of the value is only safe when the
value *is* the column. With `'XXXX' || substr(pan, 1, 4)` or `'0000' || substr(pan, 6, 4)`, the
visible tail would be a part of the PAN chosen by whoever wrote the query. So partial masking is used only for direct passthroughs, and anything
computed gets a fixed-width `XXXXXXXX`, which also hides the length. Over-masking (for example
`COUNT(DISTINCT pan)`) is the deliberate trade-off.

**Partial-mask rule: the last four digits stay visible, everything else is masked, and the length
is preserved.** [masking.py](src/glasshouse/masking.py) implements it:

| Column | Original | Masked |
|---|---|---|
| PAN | `ABCDE1234F` | `XXXXXX1234` (the brief's reference example) |
| phone | `+919876543210` | `XXXXXXXXX3210` |
| account number | `5010123456780012` | `XXXXXXXXXXXX0012` |
| fewer than 4 digits, or 4 characters or fewer | `AB12`, `ABCDEFGHIJ` | fully masked (`XXXX`, `XXXXXXXXXX`) |

*Why this interpretation.* The brief says "keep the last four characters visible" but gives
`ABCDE1234F → XXXXXX1234`. Those two statements disagree for PAN, whose last character is a
check letter. "Last four characters" would give `XXXXXX234F`. Reading it as "last four
**digits**":
- matches the explicit example exactly;
- gives byte-for-byte the same output as before for phone numbers and account numbers;
- is slightly stricter for PAN, since the check letter is no longer shown.

*Change history.* The first Task 2 version used "last four characters" (`XXXXXX234F`). It was
changed on purpose to the digits rule, and the affected test was renamed to
`test_partial_mask_keeps_last_four_digits`. `tests/test_masking.py` pins the reference example.

Masked columns can still be used in `WHERE`, `JOIN`, `GROUP BY` and `ORDER BY` (for example, look
up a customer by PAN). Only output values are masked. Masking is applied by **output position**,
so column names and the response shape are unchanged, and `masked_columns` lists the masked
output names.

### Why SQL string matching is not enough

Column-level security needs to know **which table a name refers to and where the value
ends up**, and text doesn't tell you that:
- `SELECT x.p FROM (SELECT pan AS p FROM customers) x` never mentions `pan` in the outer query.
- `zz.phone` only makes sense once `zz` is resolved to `customers`.
- `"PAN"`, `Pan` and `customers.pan` are the same column.
- A string literal `'pan'` is not a column.
- `USING (customer_id)` creates column references that aren't written in the text at all.

A regex over-blocks literals and under-blocks aliases. Lineage questions like "does this UNION
branch carry a PAN" can't be answered from text at all. The AST plus scope resolution answers
them structurally.

### Role-filtered LLM schema

`RolePolicy.prompt_schema()` renders the schema from the catalog and drops every column the
role is denied, including its hints. It also drops foreign-key hints that point to a table the
role can't see: the auditor's prompt doesn't even contain the word `customers`. Masked columns
are listed with a `[masked in output]` note. The provider interface now takes the schema:
`generate(question, schema)`. FakeProvider records the prompt it would have sent (`last_prompt`),
and tests check those prompts for each role. The database schema itself is unchanged.

### Audit trail

Every `/ask` call writes **exactly one** row to `audit_log` in a separate SQLite file
(`data/audit.db`). This includes:
- ok, refused and rejected (by the validator or the policy);
- error (malformed output, execution failure, timeout, unexpected exception);
- 403 role failures;
- **bodies FastAPI rejects**: missing fields, invalid JSON, wrong types. A
  `RequestValidationError` handler scoped to `POST /ask` records them as `rejected` with reason
  `invalid request: <field> invalid`, plus whatever role and question could be read from the raw
  body. The row has
`request_id, timestamp (UTC), role, question, generated_sql (raw provider SQL), final_status,
reason, row_count, masked_columns`.

- **One write per request.** The pipeline (`_process`) never writes to the audit log. It
  returns an outcome, even on unexpected exceptions, and `AskService.ask` then writes the single
  record through `_audit`. For a body that fails validation, the route never runs, and
  `reject_invalid_request` writes the single record through the same `_audit`. The two paths are
  mutually exclusive, so no request is recorded twice.
- **Fail closed.** If the audit write fails, the response is replaced with `status: error`,
  no rows, "audit log unavailable". No data leaves without an audit record.
- **Append-only.** The table has `BEFORE UPDATE` / `BEFORE DELETE` triggers that abort, and the
  code only ever INSERTs or SELECTs.
- **Why a separate database.** The query engine opens *only* the ledger file, read-only, and
  ATTACH is blocked by both the validator and the authorizer. `audit_log` isn't on the
  allow-list, and startup fails if the two paths are the same file. So nothing generated by the
  LLM can read the audit trail, let alone tamper with it, and a corrupted audit file can't
  affect the ledger.

`GET /audit?limit=50` returns the most recent records first. `limit` must be an integer from 1
to 500, otherwise you get a 422. A missing audit DB returns an empty list. Malformed stored
values (bad JSON, a non-numeric row count) are normalised. An unreadable or corrupt audit DB
returns 503, not 500.

### API examples

```bash
# l2_investigator: names visible, PAN/phone masked
curl -s -X POST localhost:8000/ask -H 'content-type: application/json' \
  -d '{"question": "Show identity details for high risk customers", "role": "l2_investigator"}'
# {"status":"ok","columns":["customer_id","full_name","pan","phone"],
#  "rows":[["C00010","Aadhya Khan","XXXXXX6916","XXXXXXXXX2202"], ...],
#  "masked_columns":["pan","phone"], ...}

# same question as l1_analyst -> rejected (full_name/pan/phone are denied)
curl -s -X POST localhost:8000/ask -H 'content-type: application/json' \
  -d '{"question": "Show identity details for high risk customers", "role": "l1_analyst"}'
# {"status":"rejected","reason":"access policy: role 'l1_analyst' may not access column customers.full_name", ...}

# account numbers: masked for l1/l2, rejected for auditor
curl -s -X POST localhost:8000/ask -H 'content-type: application/json' \
  -d '{"question": "Show account numbers for accounts with open alerts", "role": "auditor"}'

# no / unknown role -> HTTP 403
curl -s -i -X POST localhost:8000/ask -H 'content-type: application/json' -d '{"question": "x"}'

# recent audit records
curl -s "localhost:8000/audit?limit=5"
```

In PowerShell, use `Invoke-RestMethod` with a `ConvertTo-Json` body, or the interactive docs at
`http://localhost:8000/docs`.

### Security test commands

```bash
pytest -q                                # everything (323 tests)
pytest -q tests/test_security_api.py     # end-to-end masking/deny bypass attempts via /ask
pytest -q tests/test_access.py           # AST enforcement + lineage plans + authorizer 2nd layer
pytest -q tests/test_policy.py           # policy loading/validation, roles, filtered prompts
pytest -q tests/test_audit.py            # one-record-per-request, /audit, separation, append-only
pytest -q tests/test_request_validation.py  # 403-before-422, malformed bodies audited
pytest -q tests/test_masking.py          # last-4-digits rule incl. ABCDE1234F -> XXXXXX1234
pytest -q -k "bypass or denied"          # just the bypass/deny cases
```

The end-to-end bypass tests don't stop at checking `status` or `masked_columns`. They load
**every real PAN, phone and account number** from the ledger and assert that none of them
appears as a substring of any returned cell.

The tests were also **mutation-checked**: each mechanism was disabled in turn and the right
tests failed:

| Mechanism disabled | Tests failing |
|---|---|
| masking | 37 |
| full masking of transformed values | 20 |
| UNION-branch lineage | 4 |
| column deny check | 46 |
| scalar-subquery lineage | 2 |

### Known limitations (honest list)

- **Roles are self-asserted.** `role` comes from the request body. Production needs real
  authentication (SSO/JWT) that maps an identity to a role. `/audit` is likewise unauthenticated
  here and should be restricted to auditors.
- **Inference through predicates.** Masked columns are allowed in `WHERE`, as specified. A
  determined l2 user can learn a PAN one character at a time
  (`WHERE substr(pan,1,1) = 'A'` and counting rows). Closing that needs denying predicates on
  masked columns, or query-rate monitoring on the audit log.
- **Lineage is conservative but bounded by sqlglot.** Lineage is computed by my own walk over
  sqlglot scopes, not `sqlglot.lineage`. Constructs it can't resolve make the query
  **rejected** (fail closed), not passed. Untested shapes include recursive CTEs, `LATERAL`,
  window frames that reference other rows' masked values, and `VALUES` lists. A sqlglot upgrade
  could change scope behaviour; the mutation-checked tests are the guard for that.
- **Over-masking.** Any expression touching a masked column is fully masked, even aggregates like
  `COUNT(DISTINCT pan)`.
- **The table-wide deny for auditors** also blocks harmless counts on `customers`. That's
  intentional ("all customers columns = deny").
- **What audit can't see.** Requests rejected before FastAPI's routing, such as a wrong HTTP
  method (405) or a connection dropped mid-body, are not audited; these never reach `/ask`.
  The raw body of a malformed request is not stored, only the role and question that could be
  read from it.
- **The audit DB is append-only at the application level** (triggers). Anyone with filesystem
  access can still delete the file. Production would ship records to WORM storage or a SIEM.

## Tests

| File | Covers |
|---|---|
| `tests/test_policy.py` | Policy YAML validation (10 malformed variants), the permission matrix, no default role, role-filtered prompts (including the prompt actually sent to the provider) |
| `tests/test_access.py` | Denied columns in SELECT/WHERE/JOIN/GROUP BY/HAVING/ORDER BY/subqueries/CTEs/UNION/window/CASE/aliases/quoted identifiers; `SELECT *` rejection; lineage mask plans; the authorizer column-deny layer |
| `tests/test_security_api.py` | 15 bypass attempts × (l2 masked with no PII leak, l1 rejected); deny-anywhere cases; account number across roles; 403 role validation, with proof the provider/ledger is never reached |
| `tests/test_masking.py` | The partial-mask rule, including the brief's `ABCDE1234F → XXXXXX1234` example, phone/account/short/no-digit values, length preserved, at most 4 characters revealed, positional application, shape mismatch fails closed |
| `tests/test_request_validation.py` | Role checked first: 12 malformed or role-less bodies give 403; 4 invalid bodies with a valid role give the standard 422 plus `request_id`; exactly one audit record each; the provider is never called; other endpoints' validation is untouched and not audited |
| `tests/test_audit.py` | Exactly one record per outcome (ok/rejected/policy/refused/malformed/execution error/timeout/403/exception); `/audit` ordering and limit validation; malformed and corrupt audit data; append-only triggers; audit not reachable from `/ask`; separate files |
| `tests/test_validator.py` | 38 unsafe cases (DDL/DML, stacked statements, ATTACH/PRAGMA/VACUUM, catalogs, writes nested in CTEs, `SELECT INTO`, mixed case, comment obfuscation, dangerous functions) and 12 legitimate ones, including `narration LIKE '%drop%'` and SQL inside string literals |
| `tests/test_executor.py` | AST row-cap rewriting, 200/1000 caps on real data, the 2 s timeout (< 3 s total), read-only connection, the authorizer blocking catalog and non-allow-listed reads when the validator is bypassed |
| `tests/test_api.py` | README examples return the planted accounts; destructive fixtures are `rejected` with row counts unchanged; malformed provider output gives `error`, never 500; timeout and row cap through the endpoint; refusals; prompt contains the schema |
| `tests/test_generate.py` | same seed gives identical counts and checksum; CLI entry point; minimum volumes and 180-day span; each planted pattern meets its definition |

## Layout

```
config/access_policy.yaml   role -> column permissions (Task 2)
src/glasshouse/
  config.py      settings + allow-list + high-risk countries
  schema.py      DDL, column catalog parsed from it, role-filterable prompt schema
  generate.py    seeded data generator (python -m glasshouse.generate)
  provider.py    SQLGeneration model, LLMProvider, FakeProvider, OllamaProvider, prompt
  validator.py   sqlglot-based safety validator
  policy.py      access policy loading/validation, role lookup        (Task 2)
  access.py      AST column enforcement + output lineage              (Task 2)
  masking.py     value masking by output position                     (Task 2)
  audit.py       append-only audit log, separate SQLite file          (Task 2)
  executor.py    read-only guarded execution, row cap, timeout, column-deny authorizer
  service.py     /ask pipeline, single audit write per request
  app.py         FastAPI app factory, /ask, /audit, /health
  fixtures/fake_responses.json
scripts/build_fixtures.py   regenerates the fixture file
tests/
```
