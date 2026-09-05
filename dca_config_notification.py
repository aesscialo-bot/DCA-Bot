"""Readback verification and credential-free configuration workflow receipts.

Notification delivery is deliberately separate from the rules write. A failed
receipt never retries a configuration operation or exposes an HTTP error body.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from dca_config import ALLOWED_TARGETS, validate_analysis_state, validate_rules_map


def verify_readback(expected: str, observed: str) -> None:
    """Accept only a complete, valid map matching the intended persisted rules."""

    try:
        matches = validate_rules_map(expected) == validate_rules_map(observed)
    except Exception:
        raise ValueError("Configuration readback could not be validated") from None
    if not matches:
        raise ValueError("Configuration readback did not match the intended update")


def verify_analysis_readback(expected: str, observed: str) -> None:
    try:
        matches = validate_analysis_state(expected) == validate_analysis_state(observed)
    except Exception:
        raise ValueError("Analysis invalidation readback could not be validated") from None
    if not matches:
        raise ValueError("Analysis invalidation readback did not match")


def validated_run_url(repository: str, run_id: str, run_url: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", repository):
        raise ValueError("Invalid workflow repository")
    if repository.split("/")[1] in {".", ".."}:
        raise ValueError("Invalid workflow repository")
    if not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise ValueError("Invalid workflow run identifier")
    expected = f"https://github.com/{repository}/actions/runs/{run_id}"
    if run_url != expected:
        raise ValueError("Workflow result URL is not the expected GitHub run")
    return expected


def notification_payload(
    *,
    action: str,
    symbol: str,
    outcome: str,
    verified_result: str,
    repository: str,
    run_id: str,
    run_url: str,
) -> dict:
    """Only fixed wording, allowlisted labels, and a verified run URL may escape."""

    run_url = validated_run_url(repository, run_id, run_url)
    supported_bulk = action == "set_enabled" and symbol == "all"
    target = (
        "all four targets (BTC, ETH, SOL, DOGE)" if supported_bulk
        else symbol if symbol in ALLOWED_TARGETS else "configuration request"
    )
    operation = {
        "set_amounts": "budgets",
        "set_enabled": "enable/disable setting",
        "dry_run": "budget validation",
    }.get(action, "configuration")
    if outcome == "success" and verified_result == "applied" and action in {
        "set_amounts", "set_enabled"
    } and (symbol in ALLOWED_TARGETS or supported_bulk):
        status = "APPLIED — persisted rules readback matched the requested update."
        next_step = (
            "Check `show status`. Enabling waits for the next successful analysis; "
            "this receipt does not mean a purchase was made."
        )
    elif (
        outcome == "success"
        and verified_result == "validated"
        and action == "dry_run"
        and symbol in ALLOWED_TARGETS
    ):
        status = "VALIDATED — dry run passed; no configuration was changed."
        next_step = "Trading settings were not changed."
    else:
        status = "NOT CONFIRMED — the configuration workflow did not verify completion."
        next_step = (
            "A write may already have occurred. Check `show status` and the workflow "
            "before retrying; do not repeat an enable request blindly."
        )
    return {
        "content": (
            f"DCA configuration · {target} · {operation}\n"
            f"{status}\n{next_step}\nWorkflow: <{run_url}>"
        ),
        "allowed_mentions": {"parse": [], "users": [], "roles": [], "replied_user": False},
    }


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send_notification(webhook_url: str, payload: dict, *, opener=None) -> bool:
    """Make one bounded delivery attempt without forwarding secrets on redirects."""

    try:
        parsed = urlsplit(webhook_url)
        if (
            parsed.scheme != "https"
            or parsed.netloc not in {"discord.com", "discordapp.com"}
            or parsed.query
            or parsed.fragment
            or not re.fullmatch(r"/api(?:/v[0-9]+)?/webhooks/[0-9]+/[A-Za-z0-9_-]+", parsed.path)
        ):
            return False
        request = Request(
            webhook_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "DCA-configuration-receipt"},
            method="POST",
        )
        opener = opener or build_opener(_NoRedirects())
        with opener.open(request, timeout=15) as response:
            return 200 <= response.status < 300
    except Exception:
        # HTTP and transport exceptions can embed the secret webhook URL/body.
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("verify-readback", "verify-analysis-readback"):
        verify = commands.add_parser(name)
        verify.add_argument("--expected", required=True)
        verify.add_argument("--observed", required=True)
    commands.add_parser("notify")
    args = parser.parse_args(argv)
    if args.command in {"verify-readback", "verify-analysis-readback"}:
        try:
            verifier = verify_readback if args.command == "verify-readback" else verify_analysis_readback
            verifier(
                Path(args.expected).read_text(encoding="utf-8"),
                Path(args.observed).read_text(encoding="utf-8"),
            )
        except Exception:
            print("::error::Configuration readback verification failed; inspect status before retrying.")
            return 1
        print("Configuration readback verified.")
        return 0
    try:
        payload = notification_payload(
            action=os.environ.get("ACTION", ""),
            symbol=os.environ.get("SYMBOL", ""),
            outcome=os.environ.get("CONFIG_STEP_OUTCOME", ""),
            verified_result=os.environ.get("CONFIG_VERIFIED_RESULT", ""),
            repository=os.environ.get("GITHUB_REPOSITORY", ""),
            run_id=os.environ.get("GITHUB_RUN_ID", ""),
            run_url=os.environ.get("CONFIG_RUN_URL", ""),
        )
        delivered = send_notification(os.environ.get("DISCORD_WEBHOOK_URL", ""), payload)
    except Exception:
        delivered = False
    if not delivered:
        print(
            "::error::Discord configuration receipt delivery failed. "
            "The configuration operation was not retried; inspect its step and live status."
        )
        return 1
    print("Discord configuration receipt delivered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
