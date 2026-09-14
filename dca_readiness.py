"""Read-only Kraken funds, minimums, and recovery preflight.

This checks free GBP against one maximum enabled daily allocation plus a 1%
reserve for fees and rounding. It is an operational check, not authorization
to trade or a guarantee of future prices, balances, analysis, or scheduling.
Only boolean checks and canonical target names may reach the public output.
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
import json
import os
from typing import Mapping

from dca_config import ALLOWED_TARGETS, validate_execution_state, validate_rules_map
from kraken_client import get_kraken_exchange, get_market_minimum_gbp
from kraken_order_audit import audit_orders


BALANCE_RESERVE_FACTOR = Decimal("1.01")
# Kraken's GetApiKeyInfo permission names are documented at
# https://docs.kraken.com/api/docs/rest-api/get-api-key-info
QUERY_PERMISSIONS = frozenset({"query-funds", "query-open-trades", "query-closed-trades"})
FAILURE_STAGES = frozenset({"rules", "execution", "permissions", "balance", "quote_minimum", "order_audit"})


class PreflightError(RuntimeError):
    def __init__(self, stage, target=None):
        self.stage = stage if stage in FAILURE_STAGES else "preflight"
        self.target = target if target in ALLOWED_TARGETS else None
        super().__init__("Read-only preflight failed")


@contextmanager
def _stage(name, target=None):
    try:
        yield
    except Exception:
        raise PreflightError(name, target) from None


def _permission_checks(client) -> dict[str, bool]:
    payload = client.privatePostGetApiKeyInfo({})
    if (not isinstance(payload, Mapping) or payload.get("error") != []
            or not isinstance(payload.get("result"), Mapping)):
        raise ValueError("Kraken API-key information is unavailable")
    permissions = payload["result"].get("permissions")
    if not isinstance(permissions, list) or any(not isinstance(item, str) for item in permissions):
        raise ValueError("Kraken API-key permissions are unavailable")
    return {
        "query_permissions_ready": QUERY_PERMISSIONS.issubset(permissions),
        "order_permission_ready": "modify-trades" in permissions,
        "withdrawals_disabled": "withdraw-funds" not in permissions,
    }


def _nonnegative_decimal(value) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Invalid numeric preflight input")
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("Invalid numeric preflight input")
    return number


def check_readiness(rules_value, execution_value, *, exchange=None) -> dict:
    """Use read-only exchange endpoints and return sanitized operational checks."""

    with _stage("rules"):
        rules = validate_rules_map(rules_value)
    with _stage("execution"):
        execution = validate_execution_state(execution_value)
    enabled = [target for target in ALLOWED_TARGETS if rules[target]["BUY_ENABLED"]]
    pending_orders_clear = all(
        entry.get("PENDING_ORDER") is None for entry in execution.values()
    )
    pending_deliveries_clear = all(
        not entry.get("PENDING_GIST_DELIVERIES") for entry in execution.values()
    )
    with _stage("permissions"):
        client = exchange if exchange is not None else get_kraken_exchange()
        permissions = _permission_checks(client)
    if not all(permissions.values()):
        return {"checks_complete": False, "operational_preflight_passed": False,
                "failure_stage": "permissions", **permissions}
    with _stage("balance"):
        balance = client.fetch_balance()
        if not isinstance(balance, Mapping) or not isinstance(balance.get("free"), Mapping):
            raise ValueError("Kraken free balance is unavailable")
        free_gbp = _nonnegative_decimal(balance["free"].get("GBP"))
    maximum_daily = sum(
        (_nonnegative_decimal(rules[target]["REGIME_AMOUNTS_GBP"]["UP"])
         for target in enabled),
        Decimal(0),
    )
    funds_ready = free_gbp >= maximum_daily * BALANCE_RESERVE_FACTOR
    targets = {}
    for target in enabled:
        with _stage("quote_minimum", target):
            minimum = get_market_minimum_gbp(target, exchange=client)
            required = _nonnegative_decimal(minimum["effective_minimum_gbp"])
            low = _nonnegative_decimal(rules[target]["REGIME_AMOUNTS_GBP"]["LOW"])
        targets[target] = {"minimum_ready": low >= required}

    with _stage("order_audit"):
        audit = audit_orders(exchange=client)
        if not isinstance(audit, Mapping):
            raise ValueError("Kraken order audit is unavailable")
    no_unresolved_orders = (
        type(audit.get("unresolved_bot_orders")) is int
        and audit["unresolved_bot_orders"] == 0
    )
    timestamps_known = (
        type(audit.get("unknown_timestamp_closed_bot_orders")) is int
        and audit["unknown_timestamp_closed_bot_orders"] == 0
    )
    flow_integrity = audit.get("flow_integrity_ok") is True
    result = {
        "checks_complete": True,
        "enabled_targets_present": bool(enabled),
        "credentials_readable": True,
        **permissions,
        "pending_orders_clear": pending_orders_clear,
        # Reporting backlogs are visible but never gate trade eligibility.
        "pending_deliveries_clear": pending_deliveries_clear,
        "funds_ready": funds_ready,
        "no_unresolved_orders": no_unresolved_orders,
        "order_timestamps_known": timestamps_known,
        "flow_integrity_ok": flow_integrity,
        "targets": targets,
    }
    result["operational_preflight_passed"] = (
        bool(enabled)
        and pending_orders_clear
        and funds_ready
        and no_unresolved_orders
        and timestamps_known
        and flow_integrity
        and all(item["minimum_ready"] for item in targets.values())
    )
    return result


def main() -> int:
    try:
        result = check_readiness(
            os.environ.get("DCA_TARGET_MAP", ""),
            os.environ.get("DCA_EXECUTION_STATE", ""),
        )
    except Exception as error:
        # Provider/configuration exceptions can contain balances or credentials.
        failure = {"checks_complete": False, "operational_preflight_passed": False,
                   "failure_stage": error.stage if isinstance(error, PreflightError) else "preflight"}
        if isinstance(error, PreflightError) and error.target is not None:
            failure["target"] = error.target
        print(json.dumps(failure, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["operational_preflight_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
