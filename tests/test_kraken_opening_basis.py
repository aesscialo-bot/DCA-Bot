import hashlib
import json
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from github_contents import RepositoryFile
from ghostfolio import kraken_opening_basis as basis


CUTOVER_SECONDS = Decimal(str(basis.utc_timestamp(basis.CUTOVER_AT, "cutover").timestamp()))
TRADE_TIME = basis.decimal_text(CUTOVER_SECONDS - 120)
FUNDING_TIME = basis.decimal_text(CUTOVER_SECONDS - 240)


def evidence(records):
    ordered = sorted(records, key=lambda row: row["id"])
    page = {
        "page": 1,
        "offset": 0,
        "returned_count": len(ordered),
        "response_count": len(ordered),
        "record_ids": [row["id"] for row in ordered],
        "canonical_hash": basis.canonical_hash(ordered),
    }
    return basis.history_evidence(ordered, page_count=1, pages=[page])


def source_reference():
    return {
        "path": "outbox/kraken_opening_basis_source_v1.json",
        "blob_sha": "b" * 40,
        "repository_commit_sha": "c" * 40,
        "canonical_hash": "d" * 64,
        "producer_commit": "e" * 40,
    }


def opening(**quantities):
    values = {
        "BTC_GBP": Decimal("0"),
        "HYPE_USD": Decimal("0"),
        "SOL_GBP": Decimal("0"),
    }
    values.update({key: Decimal(value) for key, value in quantities.items()})
    return basis.OpeningBinding(
        repository_commit_sha="a" * 40,
        holdings_path="outbox/holdings.json",
        holdings_blob_sha="b" * 40,
        holdings_snapshot_hash="1" * 64,
        events_path="outbox/events.jsonl",
        events_blob_sha="c" * 40,
        events_content_sha256="2" * 64,
        event_prefix_hash="3" * 64,
        accepted_event_count=3,
        opening_state_hash=basis.REVIEWED_OPENING_STATE_HASH,
        quantities=values,
    )


def signed_holdings(**quantities):
    contracts = {
        "BTC_GBP": ("BTC", "BTC/GBP", "GBP"),
        "HYPE_USD": ("HYPE", "HYPE/USD", "USD"),
        "SOL_GBP": ("SOL", "SOL/GBP", "GBP"),
    }
    snapshot = {
        "version": 1,
        "as_of": "2026-08-08T10:00:00Z",
        "holdings": {
            target: {
                "asset": asset,
                "pair": pair,
                "quantity": str(quantities[target]),
                "quote_currency": quote,
                "unit_price_quote": "1",
            }
            for target, (asset, pair, quote) in contracts.items()
        },
        "unsupported_nonzero_assets": [],
    }
    snapshot["canonical_hash"] = basis.canonical_hash(snapshot)
    return basis.canonical(snapshot)


def portfolio_event(identifier, target, quantity, occurred_at):
    contracts = {
        "BTC_GBP": ("BTC", "GBP", "DIRECT_GBP"),
        "HYPE_USD": ("HYPE", "USD", "GBP_TO_USD"),
        "SOL_GBP": ("SOL", "GBP", "DIRECT_GBP"),
    }
    asset, quote, route = contracts[target]
    event = {
        "event_version": 3,
        "event_id": identifier,
        "occurred_at": occurred_at,
        "target": target,
        "base_currency": asset,
        "quote_currency": quote,
        "budget_currency": "GBP",
        "funding_order_id": f"F-{identifier}" if route == "GBP_TO_USD" else None,
        "crypto_order_id": identifier,
        "gbp_debit": "1",
        "gbp_usd_rate": "1.25" if route == "GBP_TO_USD" else "0",
        "funded_usd": "1.25" if route == "GBP_TO_USD" else "0",
        "route": route,
        "crypto_cost_quote": "1",
        "crypto_quantity": str(quantity),
        "unit_price_quote": "1",
        "funding_fee_quote": "0",
        "crypto_fee_quote": "0",
    }
    event["canonical_hash"] = basis.canonical_hash(event)
    return event


def reviewed_opening_files():
    events = [
        portfolio_event(
            "O-BTC-1", "BTC_GBP", "0.0004156777544144884",
            "2026-08-07T01:00:00Z",
        ),
        portfolio_event(
            "O-HYPE-1", "HYPE_USD", "0.2989365",
            "2026-08-07T02:00:00Z",
        ),
        portfolio_event(
            "O-SOL-1", "SOL_GBP", "0.32522054",
            "2026-08-07T03:00:00Z",
        ),
    ]
    return (
        signed_holdings(
            BTC_GBP="0.00051023",
            HYPE_USD="0.4989369",
            SOL_GBP="0.37422096",
        ),
        "".join(basis.canonical(event) + "\n" for event in events),
    )


def order(order_id, target, quote, when=TRADE_TIME, purpose="buy", status="closed"):
    occurred_at = basis.timestamp_text(when)
    expected_buy, expected_funding = basis._expected_client_ids(
        target, quote, occurred_at
    )
    return {
        "id": order_id,
        "cl_ord_id": expected_buy if purpose == "buy" else expected_funding,
        "status": status,
    }


def direct_fixture():
    trades = evidence([
        {
            "id": "T-BTC",
            "ordertxid": "O-BTC",
            "pair": "BTCGBP",
            "time": TRADE_TIME,
            "type": "buy",
            "vol": "0.1",
            "cost": "5",
            "ledgers": ["L-BTC", "L-GBP"],
        }
    ])
    ledgers = evidence([
        {
            "id": "L-BTC", "refid": "T-BTC", "time": TRADE_TIME,
            "type": "trade", "subtype": None, "asset": "BTC",
            "amount": "0.1", "fee": "0.001", "balance": "0.099",
        },
        {
            "id": "L-GBP", "refid": "T-BTC", "time": TRADE_TIME,
            "type": "trade", "subtype": None, "asset": "GBP",
            "amount": "-5", "fee": "0.1", "balance": "0",
        },
    ])
    orders = evidence([order("O-BTC", "BTC_GBP", "GBP")])
    return trades, ledgers, orders


def usd_fixture(*, commingled=False, residual=False):
    buy_cost = "4.89" if residual else "4.9"
    buy_balance = "0.01" if residual else "0"
    trades = evidence([
        {
            "id": "T-FUND", "ordertxid": "O-FUND", "pair": "GBPUSD",
            "time": FUNDING_TIME, "type": "sell", "vol": "4", "cost": "5",
            "ledgers": ["L-FUND-GBP", "L-FUND-USD"],
        },
        {
            "id": "T-HYPE", "ordertxid": "O-HYPE", "pair": "HYPEUSD",
            "time": TRADE_TIME, "type": "buy", "vol": "0.2", "cost": buy_cost,
            "ledgers": ["L-HYPE", "L-HYPE-USD"],
        },
    ])
    rows = [
        {
            "id": "L-FUND-GBP", "refid": "T-FUND", "time": FUNDING_TIME,
            "type": "trade", "subtype": None, "asset": "GBP",
            "amount": "-4", "fee": "0", "balance": "6",
        },
        {
            "id": "L-FUND-USD", "refid": "T-FUND", "time": FUNDING_TIME,
            "type": "trade", "subtype": None, "asset": "USD",
            "amount": "5", "fee": "0.1", "balance": "4.9",
        },
        {
            "id": "L-HYPE", "refid": "T-HYPE", "time": TRADE_TIME,
            "type": "trade", "subtype": None, "asset": "HYPE",
            "amount": "0.2", "fee": "0.001", "balance": "0.199",
        },
        {
            "id": "L-HYPE-USD", "refid": "T-HYPE", "time": TRADE_TIME,
            "type": "trade", "subtype": None, "asset": "USD",
            "amount": f"-{buy_cost}", "fee": "0", "balance": buy_balance,
        },
    ]
    if commingled:
        rows.append({
            "id": "L-USD-DEPOSIT", "refid": "D-USD",
            "time": basis.decimal_text(CUTOVER_SECONDS - 180),
            "type": "deposit", "subtype": None, "asset": "USD",
            "amount": "1", "fee": "0", "balance": "5.9",
        })
    ledgers = evidence(rows)
    orders = evidence([
        order("O-FUND", "HYPE_USD", "USD", purpose="funding"),
        order("O-HYPE", "HYPE_USD", "USD"),
    ])
    return trades, ledgers, orders


def build(binding, histories, **kwargs):
    trades, ledgers, orders = histories
    return basis.build_artifact(
        binding,
        trades,
        ledgers,
        orders,
        generated_at="2026-08-08T00:00:00Z",
        access=basis.AccessEvidence("0", "0", "0"),
        source_evidence=source_reference(),
        **kwargs,
    )


class ApiInfoExchange:
    def __init__(self, **overrides):
        self.result = {
            "permissions": ["query-closed-trades", "query-ledger"],
            "queryFrom": 0,
            "queryTo": 0,
            "validUntil": 0,
        }
        self.result.update(overrides)

    def privatePostGetApiKeyInfo(self, _params):
        return {"error": [], "result": self.result}


class KrakenOpeningBasisTests(unittest.TestCase):
    def test_utc_timestamp_rejects_noncanonical_but_parseable_text(self):
        for value in (
            "2026-08-08 14:24:00Z",
            "2026-08-08T14:24:00.1234567Z",
            "2026-08-08T14:24:00+00:00",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(
                basis.OpeningBasisError, "canonical UTC timestamp"
            ):
                basis.utc_timestamp(value, "generated_at")

    def test_timestamp_rounding_is_decimal_half_up_at_microseconds(self):
        self.assertEqual(
            basis.timestamp_text("1770000000.1234565"),
            "2026-02-02T02:40:00.123457Z",
        )

    def test_wire_constants_are_fixed_and_distinguish_performance_basis(self):
        self.assertEqual(basis.VERSION, 1)
        self.assertEqual(basis.BASIS_TYPE, "performance_book_cost")
        self.assertEqual(basis.METHOD, "kraken-pre-cutover-weighted-average-v1")
        self.assertEqual(set(basis.TARGETS), {"BTC_GBP", "HYPE_USD", "SOL_GBP"})
        self.assertEqual(
            basis.REVIEWED_OPENING_REPOSITORY_COMMIT_SHA,
            "b69734117ba55cf74724bc0a208dd941b971b62d",
        )

    def test_reviewed_opening_binding_records_exact_commit_files_and_hashes(self):
        holdings, events = reviewed_opening_files()
        binding = basis.derive_opening_binding(
            holdings,
            events,
            repository_commit_sha=basis.REVIEWED_OPENING_REPOSITORY_COMMIT_SHA,
            holdings_path="portfolio/kraken_holdings_snapshot_v1.json",
            holdings_blob_sha="a" * 40,
            events_path="portfolio/kraken_usd_dca_ghostfolio_events.jsonl",
            events_blob_sha="b" * 40,
        )

        self.assertEqual(binding.opening_state_hash, basis.REVIEWED_OPENING_STATE_HASH)
        self.assertEqual(binding.accepted_event_count, 3)
        self.assertEqual(binding.quantities, {
            "BTC_GBP": Decimal("0.0000945522"),
            "HYPE_USD": Decimal("0.2000004"),
            "SOL_GBP": Decimal("0.04900042"),
        })
        self.assertEqual(
            binding.events_content_sha256,
            hashlib.sha256(events.encode("utf-8")).hexdigest(),
        )

        artifact = build(binding, direct_fixture())
        self.assertEqual(artifact["opening_binding"], {
            "repository_commit_sha": basis.REVIEWED_OPENING_REPOSITORY_COMMIT_SHA,
            "opening_state_hash": basis.REVIEWED_OPENING_STATE_HASH,
            "holdings": {
                "path": "portfolio/kraken_holdings_snapshot_v1.json",
                "blob_sha": "a" * 40,
                "canonical_hash": binding.holdings_snapshot_hash,
            },
            "events": {
                "path": "portfolio/kraken_usd_dca_ghostfolio_events.jsonl",
                "blob_sha": "b" * 40,
                "content_sha256": hashlib.sha256(events.encode("utf-8")).hexdigest(),
                "prefix_hash": binding.event_prefix_hash,
                "accepted_event_count": 3,
            },
        })
        self.assertEqual(
            basis._basis_summary(artifact)["opening_binding"],
            artifact["opening_binding"],
        )

    def test_basis_uses_reviewed_commit_despite_later_production_rounding(self):
        reviewed_holdings, reviewed_events = reviewed_opening_files()
        later_event = portfolio_event(
            "O-BTC-2", "BTC_GBP", "0.0005146793565464986",
            "2026-08-08T13:11:12Z",
        )
        later_events = reviewed_events + basis.canonical(later_event) + "\n"
        later_holdings = signed_holdings(
            BTC_GBP="0.00102491",
            HYPE_USD="0.4989369",
            SOL_GBP="0.37422096",
        )
        with self.assertRaisesRegex(
            basis.OpeningBasisError, "reviewed commitment"
        ):
            basis.derive_opening_binding(
                later_holdings,
                later_events,
                repository_commit_sha="c" * 40,
                holdings_path="outbox/holdings.json",
                holdings_blob_sha="d" * 40,
                events_path="outbox/events.jsonl",
                events_blob_sha="e" * 40,
            )

        trades, ledgers, orders = direct_fixture()
        source = basis.build_source_artifact(
            basis.AccessEvidence("0", "0", "0"), trades, ledgers, orders,
            generated_at="2026-08-08T00:00:00Z", producer_commit="a" * 40,
        )
        source_commit = "c" * 40

        class Repository:
            def __init__(self):
                self.reads = []

            def read_text_at_commit(self, path, commit):
                self.reads.append((path, commit))
                if path.endswith(basis.OPENING_BASIS_SOURCE_FILE):
                    if commit != source_commit:
                        raise AssertionError("source evidence was not commit-pinned")
                    return RepositoryFile(json.dumps(source), "d" * 40, True)
                if commit != basis.REVIEWED_OPENING_REPOSITORY_COMMIT_SHA:
                    raise AssertionError("opening evidence used the mutable source commit")
                if path.endswith("holdings.json"):
                    return RepositoryFile(reviewed_holdings, "e" * 40, True)
                if path.endswith("events.jsonl"):
                    return RepositoryFile(reviewed_events, "f" * 40, True)
                raise AssertionError(path)

        repository = Repository()
        environment = {
            "DCA_OUTBOX_AUDIT_PATH": "outbox/audit.md",
            "DCA_OUTBOX_EVENT_PATH": "outbox/events.jsonl",
            "DCA_OUTBOX_HOLDINGS_PATH": "outbox/holdings.json",
            "DCA_OUTBOX_OPENING_BASIS_SOURCE_PATH": (
                "outbox/kraken_opening_basis_source_v1.json"
            ),
            "DCA_OUTBOX_OPENING_BASIS_PATH": "outbox/kraken_opening_basis_v1.json",
        }
        with patch.dict("os.environ", environment, clear=False):
            artifact = basis._build_basis_at_commit(
                repository,
                pinned_commit=source_commit,
                generated_at="2026-08-08T00:00:00Z",
                funding_links={},
            )

        self.assertEqual(
            artifact["opening_binding"]["repository_commit_sha"],
            basis.REVIEWED_OPENING_REPOSITORY_COMMIT_SHA,
        )
        self.assertEqual(
            artifact["source_evidence"]["repository_commit_sha"], source_commit
        )
        self.assertEqual(
            artifact["opening_binding"]["opening_state_hash"],
            basis.REVIEWED_OPENING_STATE_HASH,
        )

    def test_reviewed_opening_binding_fails_closed_on_invalid_identity_or_state(self):
        holdings, events = reviewed_opening_files()
        common = {
            "repository_commit_sha": basis.REVIEWED_OPENING_REPOSITORY_COMMIT_SHA,
            "holdings_path": "outbox/holdings.json",
            "holdings_blob_sha": "a" * 40,
            "events_path": "outbox/events.jsonl",
            "events_blob_sha": "b" * 40,
        }
        for field, value, message in (
            ("repository_commit_sha", "A" * 40, "repository commit"),
            ("holdings_path", "../holdings.json", "holdings path"),
            ("events_path", "outbox/holdings.json", "events path"),
            ("events_blob_sha", "b" * 39, "events blob"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                basis.OpeningBasisError, message
            ):
                basis.derive_opening_binding(
                    holdings, events, **{**common, field: value}
                )

        changed = json.loads(holdings)
        changed["holdings"]["BTC_GBP"]["quantity"] = "0.00051024"
        changed.pop("canonical_hash")
        changed["canonical_hash"] = basis.canonical_hash(changed)
        with self.assertRaisesRegex(
            basis.OpeningBasisError, "reviewed commitment"
        ):
            basis.derive_opening_binding(
                basis.canonical(changed), events, **common
            )

    def test_history_hash_is_order_independent_and_binds_exact_records(self):
        records = [{"id": "T2", "cost": "2"}, {"id": "T1", "cost": "1"}]
        result = basis.history_evidence(records, page_count=1)
        expected = hashlib.sha256(json.dumps(
            sorted(records, key=lambda row: row["id"]),
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        self.assertEqual(result.canonical_hash, expected)
        self.assertEqual(result.metadata()["record_count"], 2)

    def test_permission_window_and_expiry_are_fail_closed(self):
        for overrides, message in (
            ({"queryFrom": 1}, "unrestricted history start"),
            ({"queryTo": int(CUTOVER_SECONDS) - 1}, "ends before the cutover"),
            ({"validUntil": int(CUTOVER_SECONDS)}, "expired before artifact"),
            ({"permissions": ["query-closed-trades"]}, "query-ledger"),
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(basis.OpeningBasisError, message):
                    basis.ensure_history_permissions(
                        ApiInfoExchange(**overrides),
                        cutover_at=basis.CUTOVER_AT,
                        generated_at="2026-08-08T00:00:00Z",
                    )

    def test_api_key_null_bounds_are_present_unrestricted_defaults(self):
        for field in ("queryFrom", "queryTo", "validUntil"):
            with self.subTest(field=field):
                access = basis.ensure_history_permissions(
                    ApiInfoExchange(**{field: None}),
                    cutover_at=basis.CUTOVER_AT,
                    generated_at="2026-08-08T00:00:00Z",
                )
                self.assertEqual(access, basis.AccessEvidence("0", "0", "0"))

        cutover = int(CUTOVER_SECONDS)
        generated = int(basis.epoch_decimal(basis.utc_timestamp(
            "2026-08-08T00:00:00Z", "generated_at"
        )))
        for transform in (lambda value: value, str):
            with self.subTest(representation=transform.__name__):
                access = basis.ensure_history_permissions(
                    ApiInfoExchange(
                        queryFrom=transform(0),
                        queryTo=transform(cutover),
                        validUntil=transform(generated),
                    ),
                    cutover_at=basis.CUTOVER_AT,
                    generated_at="2026-08-08T00:00:00Z",
                )
                self.assertEqual(access, basis.AccessEvidence(
                    "0", str(cutover), str(generated)
                ))

    def test_api_key_all_omitted_bounds_are_jointly_unrestricted(self):
        exchange = ApiInfoExchange()
        for field in ("queryFrom", "queryTo", "validUntil"):
            exchange.result.pop(field)
        self.assertEqual(
            basis.ensure_history_permissions(
                exchange,
                cutover_at=basis.CUTOVER_AT,
                generated_at="2026-08-08T00:00:00Z",
            ),
            basis.AccessEvidence("0", "0", "0"),
        )

    def test_api_key_partially_present_bounds_are_incomplete(self):
        fields = ("queryFrom", "queryTo", "validUntil")
        for presence_bitmap in range(1, (1 << len(fields)) - 1):
            exchange = ApiInfoExchange()
            present = {
                field
                for index, field in enumerate(fields)
                if presence_bitmap & (1 << index)
            }
            for field in set(fields).difference(present):
                exchange.result.pop(field)
            with self.subTest(present=sorted(present)), self.assertRaisesRegex(
                basis.OpeningBasisError, "history bounds are incomplete"
            ):
                basis.ensure_history_permissions(
                    exchange,
                    cutover_at=basis.CUTOVER_AT,
                    generated_at="2026-08-08T00:00:00Z",
                )

    def test_api_key_bounds_reject_noncanonical_or_noninteger_values(self):
        malformed = (
            True, False, "None", -1, "-1", 0.5, "0.5", 0.0, "0.0", "00", " 0"
        )
        for field in ("queryFrom", "queryTo", "validUntil"):
            for value in malformed:
                with self.subTest(field=field, value=value), self.assertRaisesRegex(
                    basis.OpeningBasisError, rf"{field} is invalid"
                ):
                    basis.ensure_history_permissions(
                        ApiInfoExchange(**{field: value}),
                        cutover_at=basis.CUTOVER_AT,
                        generated_at="2026-08-08T00:00:00Z",
                    )

    def test_exact_cutover_rejects_one_microsecond_after(self):
        class Exchange:
            def __init__(self, time):
                self.time = time

            def privatePostTradesHistory(self, _params):
                return {"error": [], "result": {"count": 1, "trades": {
                    "T1": {
                        "ordertxid": "O1", "pair": "BTCGBP", "time": self.time,
                        "type": "buy", "vol": "0.1", "cost": "5",
                        "ledgers": ["L1", "L2"],
                    }
                }}}

        accepted = basis._fetch_pages(
            Exchange(str(CUTOVER_SECONDS)), method_name="privatePostTradesHistory",
            container="trades", end=int(CUTOVER_SECONDS),
            exact_end=CUTOVER_SECONDS, page_size=100,
        )
        self.assertEqual(len(accepted.records), 1)
        with self.assertRaisesRegex(basis.OpeningBasisError, "exceeds the cutover"):
            basis._fetch_pages(
                Exchange(str(CUTOVER_SECONDS + Decimal("0.000001"))),
                method_name="privatePostTradesHistory", container="trades",
                end=int(CUTOVER_SECONDS), exact_end=CUTOVER_SECONDS, page_size=100,
            )

    def test_strict_pagination_binds_each_page_and_detects_count_drift(self):
        rows = {
            f"T{i}": {
                "ordertxid": f"O{i}", "pair": "BTCGBP", "time": TRADE_TIME,
                "type": "buy", "vol": "0.1", "cost": "5",
                "ledgers": [f"L{i}A", f"L{i}B"],
            }
            for i in range(3)
        }

        class Exchange:
            def __init__(self, drift=False):
                self.drift = drift

            def privatePostTradesHistory(self, params):
                offset = params["ofs"]
                ids = sorted(rows)[offset:offset + 2]
                count = 4 if self.drift and offset else 3
                return {"error": [], "result": {
                    "count": count, "trades": {key: rows[key] for key in ids},
                }}

        result = basis._fetch_pages(
            Exchange(), method_name="privatePostTradesHistory", container="trades",
            end=int(CUTOVER_SECONDS), exact_end=CUTOVER_SECONDS, page_size=2,
        )
        self.assertEqual(result.page_count, 2)
        self.assertEqual([page["offset"] for page in result.pages], [0, 2])
        with self.assertRaisesRegex(basis.OpeningBasisError, "count changed"):
            basis._fetch_pages(
                Exchange(drift=True), method_name="privatePostTradesHistory",
                container="trades", end=int(CUTOVER_SECONDS),
                exact_end=CUTOVER_SECONDS, page_size=2,
            )

    def test_source_artifact_has_exact_normalized_schema_and_detects_tampering(self):
        trades, ledgers, orders = direct_fixture()
        source = basis.build_source_artifact(
            basis.AccessEvidence("0", "0", "0"), trades, ledgers, orders,
            generated_at="2026-08-08T00:00:00Z", producer_commit="a" * 40,
        )
        parsed, access, parsed_trades, parsed_ledgers, parsed_orders = (
            basis.parse_source_artifact(json.dumps(source))
        )
        self.assertEqual(parsed["canonical_hash"], source["canonical_hash"])
        self.assertEqual(access, basis.AccessEvidence("0", "0", "0"))
        self.assertEqual(set(parsed_trades.records[0]), {
            "id", "ordertxid", "pair", "time", "type", "vol", "cost", "ledgers"
        })
        self.assertEqual(set(parsed_ledgers.records[0]), {
            "id", "refid", "time", "type", "subtype", "asset", "amount", "fee", "balance"
        })
        self.assertEqual(set(parsed_orders.records[0]), {"id", "cl_ord_id", "status"})
        tampered = json.loads(json.dumps(source))
        tampered["trades"][0]["cost"] = "6"
        tampered["canonical_hash"] = basis.canonical_hash({
            key: value for key, value in tampered.items() if key != "canonical_hash"
        })
        with self.assertRaisesRegex(basis.OpeningBasisError, "page hash"):
            basis.parse_source_artifact(json.dumps(tampered))

        for field in ("query_from", "query_to", "key_valid_until"):
            persisted_null = json.loads(json.dumps(source))
            persisted_null["access"][field] = None
            persisted_null["canonical_hash"] = basis.canonical_hash({
                key: value
                for key, value in persisted_null.items()
                if key != "canonical_hash"
            })
            with self.subTest(field=field), self.assertRaisesRegex(
                basis.OpeningBasisError, rf"{field} is invalid"
            ):
                basis.parse_source_artifact(json.dumps(persisted_null))

    def test_direct_gbp_basis_counts_quote_fee_and_never_double_counts_base_fee(self):
        artifact = build(opening(BTC_GBP="0.099"), direct_fixture())
        position = artifact["positions"]["BTC_GBP"]
        self.assertEqual(position["coverage"], "complete")
        self.assertEqual(position["cost_basis_gbp"], "5.1")
        self.assertEqual(position["covered_quantity"], "0.099")
        acquisition = position["acquisitions"][0]
        self.assertEqual(acquisition["base_fee_quantity"], "0.001")
        self.assertEqual(acquisition["pair"], "BTCGBP")
        self.assertEqual(acquisition["performance_cost_gbp"], "5.1")

    def test_direct_gbp_trade_and_ledger_are_complete_without_legacy_client_id(self):
        trades, ledgers, orders = direct_fixture()
        legacy_orders = evidence([{**orders.records[0], "cl_ord_id": None}])
        position = build(
            opening(BTC_GBP="0.099"), (trades, ledgers, legacy_orders)
        )["positions"]["BTC_GBP"]

        self.assertEqual(position["coverage"], "complete")
        self.assertIsNone(position["acquisitions"][0]["client_order_id"])

    def test_base_fee_consuming_the_full_acquisition_can_never_be_complete(self):
        trades, ledgers, orders = direct_fixture()
        rows = [dict(row) for row in ledgers.records]
        next(row for row in rows if row["id"] == "L-BTC")["fee"] = "0.1"
        position = build(
            opening(BTC_GBP="0"), (trades, evidence(rows), orders)
        )["positions"]["BTC_GBP"]

        self.assertEqual(position["coverage"], "missing")
        self.assertEqual(position["acquisitions"], [])

    def test_usd_route_is_inferred_from_deterministic_orders_and_conserves_cash(self):
        artifact = build(opening(HYPE_USD="0.199"), usd_fixture())
        position = artifact["positions"]["HYPE_USD"]
        self.assertEqual(position["coverage"], "complete")
        self.assertEqual(position["cost_basis_gbp"], "4")
        acquisition = position["acquisitions"][0]
        self.assertEqual(acquisition["route"], "GBP_TO_USD")
        self.assertEqual(acquisition["pair"], "HYPEUSD")
        self.assertEqual(acquisition["fx"]["pair"], "GBPUSD")
        self.assertEqual(acquisition["funding_fee_quote"], "0.1")

    def test_commingled_or_residual_usd_can_never_be_complete(self):
        for fixture in (usd_fixture(commingled=True), usd_fixture(residual=True)):
            with self.subTest(kind=len(fixture[1].records)):
                position = build(opening(HYPE_USD="0.199"), fixture)["positions"]["HYPE_USD"]
                self.assertEqual(position["coverage"], "missing")
                self.assertIsNone(position["cost_basis_gbp"])
                self.assertTrue(any(item["kind"] == "unlinked_trade" for item in position["unresolved"]))

    def test_wrong_or_reused_funding_constraint_cannot_rescue_evidence(self):
        trades, ledgers, orders = usd_fixture()
        with self.assertRaisesRegex(basis.OpeningBasisError, "do not match Kraken history"):
            build(
                opening(HYPE_USD="0.199"), (trades, ledgers, orders),
                funding_links={"O-HYPE": "O-NOT-THERE"},
            )
        with self.assertRaisesRegex(basis.OpeningBasisError, "cannot support multiple"):
            basis.parse_funding_links('{"O-HYPE":"O-FUND","O-OTHER":"O-FUND"}')

    def test_target_movement_blocks_only_that_position(self):
        trades, ledgers, orders = direct_fixture()
        rows = list(ledgers.records) + [{
            "id": "L-HYPE-REWARD", "refid": "R1", "time": TRADE_TIME,
            "type": "staking", "subtype": None, "asset": "HYPE",
            "amount": "0.1", "fee": "0", "balance": "0.1",
        }]
        artifact = build(opening(BTC_GBP="0.099"), (trades, evidence(rows), orders))
        self.assertEqual(artifact["positions"]["BTC_GBP"]["coverage"], "complete")
        self.assertEqual(artifact["positions"]["HYPE_USD"]["coverage"], "missing")
        self.assertEqual(artifact["positions"]["SOL_GBP"]["coverage"], "complete")

    def test_swapped_ledger_references_fail_the_whole_artifact(self):
        trades, ledgers, orders = direct_fixture()
        rows = [dict(row) for row in ledgers.records]
        for row in rows:
            row["refid"] = "T-SWAPPED"
        with self.assertRaisesRegex(basis.OpeningBasisError, "owning trade"):
            build(opening(BTC_GBP="0.099"), (trades, evidence(rows), orders))

    def test_cross_target_ledger_reuse_fails_globally(self):
        trades, ledgers, orders = direct_fixture()
        reused_trade = {
            "id": "T-SOL", "ordertxid": "O-SOL", "pair": "SOLGBP",
            "time": TRADE_TIME, "type": "buy", "vol": "0.1", "cost": "5",
            "ledgers": ["L-BTC", "L-GBP"],
        }
        reused_orders = evidence([
            *orders.records,
            {"id": "O-SOL", "cl_ord_id": None, "status": "closed"},
        ])
        with self.assertRaisesRegex(basis.OpeningBasisError, "multiple trades"):
            build(
                opening(BTC_GBP="0.099"),
                (evidence([*trades.records, reused_trade]), ledgers, reused_orders),
            )

    def test_missing_coverage_preserves_known_acquisitions_but_no_aggregate_basis(self):
        position = build(opening(BTC_GBP="0.2"), direct_fixture())["positions"]["BTC_GBP"]
        self.assertEqual(position["coverage"], "missing")
        self.assertEqual(position["covered_quantity"], "0.099")
        self.assertEqual(position["missing_quantity"], "0.101")
        self.assertEqual(len(position["acquisitions"]), 1)
        self.assertIsNone(position["cost_basis_gbp"])

    def test_consumer_size_limit_fails_before_publication(self):
        small = basis._hash_artifact({"data": "x" * 900_000})
        self.assertLessEqual(len(basis._serialized_artifact(small).encode()), 1_000_000)
        large = basis._hash_artifact({"data": "x" * 1_000_000})
        with self.assertRaisesRegex(basis.OpeningBasisError, "consumer size limit"):
            basis._serialized_artifact(large)

    def test_publish_requires_the_exact_reviewed_preview_hash_before_writing(self):
        artifact = basis._hash_artifact({"version": 1, "purpose": "preview"})

        class Repository:
            calls = 0

            def write_once_text(self, _path, content, *, message):
                self.calls += 1
                self.message = message
                return SimpleNamespace(content=content)

        repository = Repository()
        with self.assertRaisesRegex(basis.OpeningBasisError, "reviewed"):
            basis._publish_write_once(
                artifact,
                path="outbox/kraken_opening_basis_v1.json",
                expected_filename=basis.OPENING_BASIS_FILE,
                expected_canonical_hash="0" * 64,
                message="Create immutable",
                client=repository,
            )
        self.assertEqual(repository.calls, 0)

        basis._publish_write_once(
            artifact,
            path="outbox/kraken_opening_basis_v1.json",
            expected_filename=basis.OPENING_BASIS_FILE,
            expected_canonical_hash=artifact["canonical_hash"],
            message="Create immutable",
            client=repository,
        )
        self.assertEqual(repository.calls, 1)

    def test_compact_retry_reuses_fixed_source_commit_after_write_failure(self):
        trades, ledgers, orders = direct_fixture()
        source = basis.build_source_artifact(
            basis.AccessEvidence("0", "0", "0"), trades, ledgers, orders,
            generated_at="2026-08-08T00:00:00Z", producer_commit="a" * 40,
        )

        class Repository:
            def read_text(self, _path):
                return RepositoryFile("", None, False)

            def read_text_at_commit(self, path, commit):
                self.assert_commit = commit
                if path.endswith(basis.OPENING_BASIS_SOURCE_FILE):
                    return RepositoryFile(json.dumps(source), "b" * 40, True)
                return RepositoryFile("fixture", "f" * 40, True)

        args = SimpleNamespace(
            mode="publish",
            generated_at="2026-08-08T00:00:00Z",
            expected_canonical_hash="9" * 64,
            source_commit_sha="c" * 40,
        )
        artifacts = []

        def capture(artifact, **_kwargs):
            artifacts.append(json.loads(json.dumps(artifact)))
            if len(artifacts) == 1:
                raise RuntimeError("simulated compact write failure")

        environment = {
            "DCA_OUTBOX_AUDIT_PATH": "outbox/audit.md",
            "DCA_OUTBOX_EVENT_PATH": "outbox/events.jsonl",
            "DCA_OUTBOX_HOLDINGS_PATH": "outbox/holdings.json",
            "DCA_OUTBOX_OPENING_BASIS_SOURCE_PATH": (
                "outbox/kraken_opening_basis_source_v1.json"
            ),
            "DCA_OUTBOX_OPENING_BASIS_PATH": "outbox/kraken_opening_basis_v1.json",
        }
        with patch.dict("os.environ", environment, clear=False), patch.object(
            basis, "derive_opening_binding", return_value=opening(BTC_GBP="0.099")
        ), patch.object(basis, "publish", side_effect=capture):
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                basis._run_basis(args, Repository())
            basis._run_basis(args, Repository())

        self.assertEqual(artifacts[0], artifacts[1])
        self.assertEqual(
            artifacts[0]["source_evidence"]["repository_commit_sha"], "c" * 40
        )

    def test_workflow_is_manual_only_and_exposes_reviewed_two_stage_contract(self):
        workflow = (
            Path(__file__).parents[1] / ".github" / "workflows" /
            "kraken_opening_basis.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertIn("expected_canonical_hash:", workflow)
        self.assertIn("source_commit_sha:", workflow)


if __name__ == "__main__":
    unittest.main()
