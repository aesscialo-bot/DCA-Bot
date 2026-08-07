"""Build a fail-closed Ghostfolio migration reconciliation report.

This utility never writes to Ghostfolio.  It compares recovered sources with a
local Ghostfolio export and emits deterministic ``already_present``, ``insert``,
``conflict`` and ``failed`` sections.  A separate reviewed import is required.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


PAIR_SYMBOLS = {
    "BTC_THB": "bitcoin",
    "DOGE_THB": "dogecoin",
    "LINK_THB": "chainlink",
    "SUI_THB": "sui",
    "BTC_GBP": "bitcoin",
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _decimal_text(value):
    return format(Decimal(str(value)).normalize(), "f")


def legacy_candidates(manifest):
    candidates = []
    for row in manifest.get("transactions", []):
        pair = row.get("pair")
        order_id = str(row.get("order_id") or "")
        if pair not in PAIR_SYMBOLS or not order_id:
            candidates.append({"status": "failed", "reason": "unsupported or incomplete legacy record", "source": row})
            continue
        candidate = {
            "order_id": order_id,
            "account": "Bitkub Legacy",
            "symbol": PAIR_SYMBOLS[pair],
            "currency": "USD",
            "date": row["executed_at"],
            "quantity": _decimal_text(row["amount_crypto"]),
            "unit_price": _decimal_text(row["usd_price_per_unit"]),
            "fee": "0",
            "provenance": ["recovered Bitkub legacy manifest"],
        }
        candidate["canonical_hash"] = digest(candidate)
        candidates.append(candidate)
    return candidates


def kraken_gist_candidates(markdown):
    candidates = []
    for line in markdown.splitlines():
        if not line.startswith("|") or "GBP" not in line or "BTC" not in line:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 8 or not re.fullmatch(r"[A-Z0-9-]{8,}", cells[6]):
            continue
        quantity = re.search(r"([0-9.]+)\s+BTC", cells[5])
        price = re.search(r"GBP\s+([0-9,.]+)", cells[4])
        if not quantity or not price:
            continue
        local_timestamp = cells[0]
        if re.search(r"[+-]\d{2}$", local_timestamp):
            local_timestamp += "00"
        local_date = datetime.strptime(local_timestamp, "%Y-%m-%d %H:%M %z")
        candidate = {
            "order_id": cells[6],
            "account": "Kraken DCA",
            "symbol": "bitcoin",
            "currency": "GBP",
            "date": local_date.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "quantity": _decimal_text(quantity.group(1)),
            "unit_price": _decimal_text(price.group(1).replace(",", "")),
            "fee": "0",
            "provenance": ["durable Portfolio Compass Markdown ledger"],
        }
        candidate["canonical_hash"] = digest(candidate)
        candidates.append(candidate)
    return candidates


def local_index(local_export):
    account_names = {
        account.get("id"): account.get("name")
        for account in local_export.get("accounts", [])
        if isinstance(account, dict)
    }
    result = {}
    for activity in local_export.get("activities", []):
        if not isinstance(activity, dict):
            continue
        comment = str(activity.get("comment") or "")
        for order_id in re.findall(r"[A-Za-z0-9-]{8,}", comment):
            result[order_id] = {
                "account": account_names.get(activity.get("accountId"), "unknown"),
                "symbol": activity.get("symbol"),
                "currency": activity.get("currency"),
                "date": activity.get("date"),
                "quantity": _decimal_text(activity.get("quantity", 0)),
                "unit_price": _decimal_text(activity.get("unitPrice", 0)),
                "fee": _decimal_text(activity.get("fee", 0)),
            }
    return result


def reconcile(candidates, local_export, *, source_failures=None, evidence=None):
    report = {
        "schema": "GhostfolioLegacyReconciliationV1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": "dry-run",
        "already_present": [],
        "insert": [],
        "conflict": [],
        "failed": list(source_failures or []),
        "evidence": evidence or {},
    }
    current = local_index(local_export)
    seen = set()
    for candidate in candidates:
        if candidate.get("status") == "failed":
            report["failed"].append(candidate)
            continue
        order_id = candidate["order_id"]
        if order_id in seen:
            report["conflict"].append({"order_id": order_id, "reason": "duplicate recovered order ID"})
            continue
        seen.add(order_id)
        existing = current.get(order_id)
        if existing is None:
            report["insert"].append(candidate)
            continue
        expected = {key: candidate[key] for key in ("account", "symbol", "currency", "date", "quantity", "unit_price", "fee")}
        if existing == expected:
            report["already_present"].append(candidate)
        else:
            report["conflict"].append({"order_id": order_id, "expected": expected, "existing": existing})
    report["counts"] = {key: len(report[key]) for key in ("already_present", "insert", "conflict", "failed")}
    report["can_commit"] = not report["conflict"] and not report["failed"]
    report["decision"] = "READY_FOR_REVIEW" if report["can_commit"] else "BLOCKED"
    return report


def _load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-manifest", required=True)
    parser.add_argument("--local-export", required=True)
    parser.add_argument("--kraken-ledger", required=True)
    parser.add_argument("--github-variables")
    parser.add_argument("--kraken-audit")
    parser.add_argument("--hosted-export")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    failures = []
    if not args.hosted_export:
        failures.append({"source": "hosted Ghostfolio export", "reason": "not available on this PC; migration commit is blocked"})
    elif not Path(args.hosted_export).is_file():
        failures.append({"source": "hosted Ghostfolio export", "reason": "configured path does not exist"})

    evidence = {}
    if args.github_variables:
        variables = {row["name"]: row["value"] for row in _load_json(args.github_variables)}
        execution = json.loads(variables.get("DCA_EXECUTION_STATE", "{}"))
        evidence["protected_execution_state"] = {
            "BTC_USD_LAST_BUY_DATE": execution.get("BTC_USD", {}).get("LAST_BUY_DATE", "")
        }
    if args.kraken_audit:
        text = Path(args.kraken_audit).read_text(encoding="utf-8-sig")
        matches = re.findall(r"(\{\"audit_date\".*\})", text)
        if matches:
            audit = json.loads(matches[-1])
            evidence["kraken_read_only_audit"] = {
                "audit_date": audit.get("audit_date"),
                "status": audit.get("status"),
                "unresolved_bot_orders": audit.get("unresolved_bot_orders"),
            }

    candidates = legacy_candidates(_load_json(args.legacy_manifest))
    candidates.extend(kraken_gist_candidates(Path(args.kraken_ledger).read_text(encoding="utf-8-sig")))
    report = reconcile(candidates, _load_json(args.local_export), source_failures=failures, evidence=evidence)
    Path(args.output).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(canonical({"decision": report["decision"], "counts": report["counts"], "output": args.output}))
    return 0 if report["can_commit"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
