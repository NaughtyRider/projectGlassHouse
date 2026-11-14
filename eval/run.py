"""Execution-accuracy evaluation runner.

    python -m eval.run --provider fake            # from the repo root; writes eval/report.json

How it works
------------
1. Load eval/questions.jsonl (id, role, question, gold_sql | expected outcome).
2. Build the *real* FastAPI app (create_app) with the chosen provider, the real access policy
   and a throw-away audit DB, and send every question through ``POST /ask`` with
   FastAPI's TestClient: role check, role-filtered prompt, validator, access policy, executor,
   masking and audit all run exactly as they do for an analyst. Provider SQL is never executed
   directly.
3. Compute each gold result through the same guarded path (validator -> access policy for
   the item's role -> read-only executor -> masking), so "correct" means "the analyst would
   have seen exactly the gold answer", masking included.
4. Compare result sets (eval/scoring.py), never SQL strings.
5. An execution spy wraps the executor the service calls and records every statement that
   actually reaches SQLite. Each is re-checked independently; any that fails the checks, or
   that belongs to a negative item designed to be blocked, counts as an unsafe execution.
   Ledger row counts are compared before and after the run as well.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

import glasshouse.service as service_module
from glasshouse.access import check_access
from glasshouse.app import create_app
from glasshouse.audit import AuditLog
from glasshouse.config import Settings
from glasshouse.executor import execute_query
from glasshouse.generate import generate, row_counts, transactions_checksum
from glasshouse.masking import apply_mask_plan
from glasshouse.policy import AccessPolicy, RolePolicy
from glasshouse.provider import get_provider
from glasshouse.validator import validate_sql

from eval.scoring import compare_results, gold_is_ordered, percentile

EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_DATASET = EVAL_DIR / "questions.jsonl"
DEFAULT_REPORT = EVAL_DIR / "report.json"
BLOCKING_STAGES = {"validator", "access_policy"}
REQUIRED_FIELDS = {"id", "role", "question", "expected"}


class DatasetError(Exception):
    pass


# ----------------------------------------------------------------------------- dataset

def load_dataset(path: Path | str = DEFAULT_DATASET) -> list[dict]:
    items, seen = [], set()
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        item = json.loads(line)
        missing = REQUIRED_FIELDS - set(item)
        if missing:
            raise DatasetError(f"line {n}: missing fields {sorted(missing)}")
        if item["id"] in seen:
            raise DatasetError(f"line {n}: duplicate id {item['id']}")
        seen.add(item["id"])
        if item["expected"] == "answer" and not item.get("gold_sql"):
            raise DatasetError(f"{item['id']}: answerable item needs gold_sql")
        if item["expected"] == "refused_or_rejected" and item.get("expected_stage") not in {"provider", *BLOCKING_STAGES}:
            raise DatasetError(f"{item['id']}: negative item needs expected_stage")
        if item["expected"] not in {"answer", "refused_or_rejected"}:
            raise DatasetError(f"{item['id']}: unknown expected outcome {item['expected']!r}")
        items.append(item)
    return items


# ----------------------------------------------------------------------------- gold

def gold_result(sql: str, role: RolePolicy, settings: Settings) -> tuple[list[str], list[list[Any]]]:
    """Run trusted gold SQL through the same guards the pipeline uses, for the item's role."""
    verdict = validate_sql(sql, settings.allowed_tables)
    if not verdict.ok:
        raise DatasetError(f"gold SQL fails validation: {verdict.reason}")
    decision = check_access(verdict.ast, role)
    if not decision.ok:
        raise DatasetError(f"gold SQL not permitted for role {role.name}: {decision.reason}")
    result = execute_query(verdict.ast, settings.db_path, settings.allowed_tables,
                           timeout_seconds=settings.timeout_seconds, default_limit=settings.default_limit,
                           max_limit=settings.max_limit, denied_columns=role.denied_columns())
    rows, _ = apply_mask_plan(result.columns, result.rows, decision.mask_plan)
    return result.columns, rows


# ----------------------------------------------------------------------------- execution spy

class ExecutionSpy:
    """Wraps the executor used by the /ask service and records every statement that runs."""

    def __init__(self, policy: AccessPolicy, settings: Settings):
        self.policy = policy
        self.settings = settings
        self.current: dict | None = None
        self.executions: list[dict] = []
        self._original = None

    def __enter__(self) -> "ExecutionSpy":
        self._original = service_module.execute_query

        def spy(tree, *args, **kwargs):
            item = self.current or {}
            sql = tree.sql(dialect="sqlite")
            self.executions.append({"id": item.get("id"), "sql": sql,
                                    "unsafe_reason": self.unsafe_reason(sql, item)})
            return self._original(tree, *args, **kwargs)

        service_module.execute_query = spy
        return self

    def __exit__(self, *exc) -> None:
        service_module.execute_query = self._original

    def unsafe_reason(self, sql: str, item: dict) -> str | None:
        """Independent re-check of a statement that reached execution."""
        if item.get("expected") == "refused_or_rejected" and item.get("expected_stage") in BLOCKING_STAGES:
            return f"negative item {item['id']} was designed to be blocked by the {item['expected_stage']}"
        verdict = validate_sql(sql, self.settings.allowed_tables)
        if not verdict.ok:
            return f"executed SQL fails validation: {verdict.reason}"
        try:
            role = self.policy.get_role(item.get("role"))
        except Exception:
            return "executed for an unknown role"
        decision = check_access(verdict.ast, role)
        if not decision.ok:
            return f"executed SQL violates access policy: {decision.reason}"
        return None

    @property
    def unsafe(self) -> list[dict]:
        return [e for e in self.executions if e["unsafe_reason"]]


# ----------------------------------------------------------------------------- helpers

def predicted_outcome(http_status: int, body: dict) -> str:
    status = body.get("status")
    if http_status == 403:
        return "rejected:role"
    if status == "rejected":
        return "rejected:access_policy" if str(body.get("reason", "")).startswith("access policy:") else "rejected:validator"
    if status == "refused":
        return "refused:provider"
    return str(status)


def expected_label(item: dict) -> str:
    if item["expected"] == "answer":
        return "answer"
    return f"refused_or_rejected ({item['expected_stage']})"


def _ensure_db(settings: Settings, seed: int) -> None:
    if not settings.db_path.exists():
        print(f"ledger not found at {settings.db_path}; generating with seed {seed}", file=sys.stderr)
        generate(seed, settings.db_path)


# ----------------------------------------------------------------------------- main loop

def run_eval(
    provider: str = "fake",
    db_path: Path | str | None = None,
    dataset_path: Path | str = DEFAULT_DATASET,
    audit_db_path: Path | str | None = None,
    seed: int = 42,
) -> dict:
    items = load_dataset(dataset_path)
    base = Settings.from_env()
    tmp = tempfile.TemporaryDirectory(prefix="glasshouse-eval-")
    try:
        settings = dataclasses.replace(
            base,
            provider=provider,
            db_path=Path(db_path) if db_path else base.db_path,
            audit_db_path=Path(audit_db_path) if audit_db_path else Path(tmp.name) / "eval_audit.db",
        )
        _ensure_db(settings, seed)
        policy = AccessPolicy.load(settings.policy_path)
        client = TestClient(create_app(settings, get_provider(settings), policy, AuditLog(settings.audit_db_path)))

        counts_before = row_counts(settings.db_path)
        results: list[dict] = []
        with ExecutionSpy(policy, settings) as spy:
            for item in items:
                spy.current = item
                n_exec_before = len(spy.executions)
                gold_cols = gold_rows = None
                if item["expected"] == "answer":
                    gold_cols, gold_rows = gold_result(item["gold_sql"], policy.get_role(item["role"]), settings)

                start = time.perf_counter()
                resp = client.post("/ask", json={"question": item["question"], "role": item["role"]})
                latency_ms = (time.perf_counter() - start) * 1000
                body = resp.json()
                predicted = predicted_outcome(resp.status_code, body)

                if item["expected"] == "answer":
                    if body.get("status") == "ok":
                        ordered = gold_is_ordered(item["gold_sql"])
                        correct, reason = compare_results(body["rows"], gold_rows, ordered,
                                                          body["columns"], gold_cols)
                    else:
                        ordered, correct = None, False
                        reason = f"expected an answer, got {predicted}: {body.get('reason')}"
                    stage_match = None
                else:
                    ordered = None
                    correct = body.get("status") in {"refused", "rejected"}
                    reason = None if correct else f"negative question was answered (status {body.get('status')})"
                    stage_match = predicted.split(":")[-1] == item["expected_stage"]

                results.append({
                    "id": item["id"],
                    "category": item.get("category"),
                    "role": item["role"],
                    "question": item["question"],
                    "expected_outcome": expected_label(item),
                    "predicted_outcome": predicted,
                    "correct": bool(correct),
                    "latency_ms": round(latency_ms, 2),
                    "failure_reason": reason,
                    "stage_match": stage_match,
                    "ordered_comparison": ordered,
                    "gold_row_count": None if gold_rows is None else len(gold_rows),
                    "returned_row_count": len(body.get("rows") or []),
                    "masked_columns": body.get("masked_columns", []),
                    "sql": body.get("sql"),
                    "reached_execution": len(spy.executions) > n_exec_before,
                })
            executions, unsafe = list(spy.executions), spy.unsafe
        counts_after = row_counts(settings.db_path)
        audit_records = len(AuditLog(settings.audit_db_path).recent(10_000))
    finally:
        tmp.cleanup()

    answerable = [r for r in results if r["expected_outcome"] == "answer"]
    negatives = [r for r in results if r["expected_outcome"] != "answer"]
    correct_answers = sum(r["correct"] for r in answerable)
    correct_refusals = sum(r["correct"] for r in negatives)
    latencies = [r["latency_ms"] for r in results]

    return {
        "evaluation_timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": provider,
        "dataset": {
            "path": Path(dataset_path).name,
            "sha256": hashlib.sha256(Path(dataset_path).read_bytes()).hexdigest(),
        },
        "ledger": {
            "transactions_sha256": transactions_checksum(settings.db_path),
            "row_counts": counts_after,
            "unchanged_by_eval": counts_before == counts_after,
        },
        "total_questions": len(results),
        "answerable_questions": len(answerable),
        "negative_questions": len(negatives),
        "correct_answers": correct_answers,
        "incorrect_answers": len(answerable) - correct_answers,
        "correct_refusals": correct_refusals,
        "negatives_blocked_by_validator_or_policy": sum(
            r["predicted_outcome"] in ("rejected:validator", "rejected:access_policy") for r in negatives),
        "execution_accuracy": round(correct_answers / len(answerable), 4) if answerable else 0.0,
        "correct_refusal_rate": round(correct_refusals / len(negatives), 4) if negatives else 0.0,
        "unsafe_executions": len(unsafe),
        "unsafe_execution_details": unsafe,
        "statements_executed": len(executions),
        "audit_records_written": audit_records,
        "mean_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else 0.0,
        "p95_latency_ms": round(percentile(latencies, 95), 2),
        "results": results,
    }


# ----------------------------------------------------------------------------- output

def print_summary(report: dict) -> None:
    line = "-" * 107
    print(line)
    print(f"{'id':<5}{'role':<17}{'expected':<37}{'predicted':<24}{'ok':<6}{'ms':>8}  reason")
    print(line)
    for r in report["results"]:
        reason = (r["failure_reason"] or "")[:60]
        print(f"{r['id']:<5}{r['role']:<17}{r['expected_outcome']:<37}{r['predicted_outcome']:<24}"
              f"{'PASS' if r['correct'] else 'FAIL':<6}{r['latency_ms']:>8.1f}  {reason}")
    print(line)
    print(f"provider               : {report['provider']}")
    print(f"questions              : {report['total_questions']} "
          f"({report['answerable_questions']} answerable, {report['negative_questions']} negative)")
    print(f"execution accuracy     : {report['execution_accuracy']:.1%} "
          f"({report['correct_answers']}/{report['answerable_questions']})")
    print(f"correct-refusal rate   : {report['correct_refusal_rate']:.1%} "
          f"({report['correct_refusals']}/{report['negative_questions']}; "
          f"{report['negatives_blocked_by_validator_or_policy']} blocked by validator/policy)")
    print(f"unsafe executions      : {report['unsafe_executions']}  "
          f"(statements executed: {report['statements_executed']}, ledger unchanged: "
          f"{report['ledger']['unchanged_by_eval']})")
    print(f"latency mean / p95     : {report['mean_latency_ms']:.1f} ms / {report['p95_latency_ms']:.1f} ms")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the GlassHouse execution-accuracy evaluation.")
    parser.add_argument("--provider", choices=["fake", "ollama"], default="fake")
    parser.add_argument("--db", type=Path, default=None, help="ledger DB (default: GLASSHOUSE_DB or data/glasshouse.db)")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--out", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--seed", type=int, default=42, help="seed used if the ledger has to be generated")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    report = run_eval(args.provider, args.db, args.dataset, seed=args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print_summary(report)
    print(f"report                 : {args.out}")
    return 1 if report["unsafe_executions"] else 0  # CI gate: any unsafe execution fails the run


if __name__ == "__main__":
    raise SystemExit(main())
