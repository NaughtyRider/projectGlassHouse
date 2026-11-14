# GlassHouse: guarded text-to-SQL for AML investigations

GlassHouse turns an analyst's question into SQL, checks that the SQL is safe and read-only,
runs it against a read-only copy of a synthetic AML ledger, and returns the rows.

```
question ──► LLMProvider ──► SQL validator ──► guarded executor ──► rows
             (structured     (sqlglot AST:      (read-only conn, authorizer,
              output)         1 SELECT only,     AST row cap, 2 s deadline)
                              table allow-list)
```

Status: **Task 1 (safe text-to-SQL vertical slice)** is done. Role enforcement, PII masking and
audit logging come in later tasks. `role` is accepted but not enforced yet, and
`masked_columns` is always `[]`.

## Setup and run

Requires Python 3.11+. No API keys or network access are needed.

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

python -m glasshouse.generate --seed 42      # builds data/glasshouse.db (~1 s)
pytest                                        # 113 tests, offline, ~15 s
uvicorn glasshouse.app:app --port 8000        # serves the API
```

```bash
curl -s -X POST localhost:8000/ask -H 'content-type: application/json' \
  -d '{"question": "Which accounts sent or received SWIFT wires involving high-risk countries?", "role": "analyst"}'
```

### Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `GLASSHOUSE_DB` | `data/glasshouse.db` | SQLite database path (used by both the generator and the API) |
| `GLASSHOUSE_PROVIDER` | `fake` | `fake` (deterministic fixtures) or `ollama` (optional local model) |
| `GLASSHOUSE_FIXTURES` | packaged `fixtures/fake_responses.json` | FakeProvider fixture file |
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

`POST /ask` with `{"question": str, "role": str}` returns:

```json
{"request_id": "hex", "status": "ok|refused|rejected|error", "sql": "...",
 "columns": [...], "rows": [[...]], "masked_columns": [], "reason": null}
```

| status | when |
|---|---|
| `ok` | the query ran. `sql` is the SQL as executed (with the row cap applied) |
| `refused` | the provider declined (including FakeProvider's answer to unknown questions) |
| `rejected` | the validator blocked the SQL. It never reached the database |
| `error` | malformed provider output, provider failure, execution failure, or `timeout: ...` |

The handler never returns HTTP 500. Any unexpected exception becomes `status: error`.
A request body that fails schema validation, such as an empty question, gets FastAPI's standard 422.
`GET /health` reports the provider and whether the database exists.

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

## Tests

| File | Covers |
|---|---|
| `tests/test_validator.py` | 38 unsafe cases (DDL/DML, stacked statements, ATTACH/PRAGMA/VACUUM, catalogs, writes nested in CTEs, `SELECT INTO`, mixed case, comment obfuscation, dangerous functions) and 12 legitimate ones, including `narration LIKE '%drop%'` and SQL inside string literals |
| `tests/test_executor.py` | AST row-cap rewriting, 200/1000 caps on real data, the 2 s timeout (< 3 s total), read-only connection, the authorizer blocking catalog and non-allow-listed reads when the validator is bypassed |
| `tests/test_api.py` | README examples return the planted accounts; destructive fixtures are `rejected` with row counts unchanged; malformed provider output gives `error`, never 500; timeout and row cap through the endpoint; refusals; prompt contains the schema |
| `tests/test_generate.py` | same seed gives identical counts and checksum; CLI entry point; minimum volumes and 180-day span; each planted pattern meets its definition |

## Layout

```
src/glasshouse/
  config.py      settings + allow-list + high-risk countries
  schema.py      DDL and prompt schema
  generate.py    seeded data generator (python -m glasshouse.generate)
  provider.py    SQLGeneration model, LLMProvider, FakeProvider, OllamaProvider, prompt
  validator.py   sqlglot-based safety validator
  executor.py    read-only guarded execution, row cap, timeout
  service.py     /ask pipeline and request/response models
  app.py         FastAPI app factory
  fixtures/fake_responses.json
scripts/build_fixtures.py   regenerates the fixture file
tests/
```
