# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from sqlalchemy import func, select, update

from src.config import Config
from src.core.trading_calendar import get_effective_trading_date
from src.repositories.portfolio_repo import PortfolioRepository
from src.repositories.portfolio_market_evidence_repo import (
    PortfolioMarketEvidenceRepository,
)
from src.repositories.decision_evidence_snapshot_repo import (
    DecisionEvidenceSnapshotRepository,
)
from src.repositories.stock_repo import StockRepository
from src.schemas.decision_evidence_snapshot import DecisionEvidenceSnapshot
from src.services.decision_evidence_snapshot_service import (
    DecisionEvidenceSnapshotService,
)
from src.services.portfolio_instrument_service import PortfolioInstrumentService
from src.services.portfolio_research_product_evidence import (
    PRODUCT_EVIDENCE_SCHEMA_VERSION,
    build_product_evidence_component,
    product_evidence_from_instrument,
)
from src.services.portfolio_risk_policy_service import PortfolioRiskPolicyService
from src.services.strategy_registry_service import (
    StrategyRegistryService,
    load_strategy_manifest,
)
from src.storage import (
    DatabaseManager,
    DecisionSignalOutcomeRecord,
    DecisionSignalRecord,
    PortfolioDailySnapshot,
    PortfolioAccount,
    PortfolioInstrument,
    PortfolioPosition,
    PortfolioRiskPolicy,
)


TEST_RESEARCH_CUTOFF = datetime(2026, 8, 4, 8, 0, 0, tzinfo=timezone.utc)


class PortfolioResearchSnapshotServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "research_snapshot.db"
        os.environ["DATABASE_PATH"] = str(self.db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()
        self.repo = PortfolioRepository(db_manager=self.db)

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        self.temp_dir.cleanup()

    def _service(self, **kwargs):
        module_name = "src.services.portfolio_research_snapshot_service"
        self.assertIsNotNone(
            importlib.util.find_spec(module_name),
            "portfolio research snapshot service module is required",
        )
        module = importlib.import_module(module_name)
        return module.PortfolioResearchSnapshotService(repo=self.repo, **kwargs)

    def test_benchmark_identity_uses_current_strategy_policy(self) -> None:
        benchmarks = self._service()._benchmark_payload(
            [
                {"market": "cn"},
                {"market": "hk"},
                {"market": "us"},
            ]
        )

        self.assertEqual(
            {item["market"]: item["code"] for item in benchmarks},
            {"cn": "000300", "hk": "HSI", "us": "SPY"},
        )
        self.assertTrue(all(item["type"] == "strategy_benchmark" for item in benchmarks))

    def test_benchmark_identity_ignores_markets_outside_strategy_policy(self) -> None:
        benchmarks = self._service()._benchmark_payload(
            [{"market": "jp"}, {"market": "kr"}, {"market": "us"}]
        )

        self.assertEqual(
            [(item["market"], item["code"]) for item in benchmarks],
            [("us", "SPY")],
        )

    def test_build_freezes_only_requested_scope_and_binds_scope_hash(self) -> None:
        cutoff = datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc)
        us_account_id = self._seed_position_with_market_bar(
            account_name="US Account",
            market="us",
            symbol="AAPL",
            currency="USD",
            source="YfinanceFetcher|adjustment=adjusted",
            cutoff=cutoff,
        )
        cn_account_id = self._seed_position_with_market_bar(
            account_name="CN Account",
            market="cn",
            symbol="515880",
            currency="CNY",
            source="BaostockFetcher|adjustment=qfq",
            cutoff=cutoff,
        )
        self._seed_strategy_benchmark_bar(
            code="000300",
            source="BaostockFetcher|adjustment=qfq",
            cutoff=cutoff,
        )

        service = self._service()
        snapshot = service.build(
            cutoff=cutoff,
            scope=[
                {"account_id": cn_account_id, "market": "CN", "symbol": "515880"},
                {"account_id": cn_account_id, "market": "cn", "symbol": "515880"},
            ],
        )

        self.assertEqual(
            snapshot["scope"],
            [{"account_id": cn_account_id, "market": "cn", "symbol": "515880"}],
        )
        self.assertRegex(snapshot["scope_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual([item["account_id"] for item in snapshot["accounts"]], [cn_account_id])
        self.assertEqual(
            [(item["account_id"], item["market"], item["symbol"]) for item in snapshot["positions"]],
            [(cn_account_id, "cn", "515880")],
        )
        self.assertEqual(
            [(item["market"], item["symbol"]) for item in snapshot["instruments"]],
            [("cn", "515880")],
        )
        self.assertEqual(
            [(item["market"], item["code"]) for item in snapshot["benchmarks"]],
            [("cn", "000300")],
        )
        self.assertNotEqual(us_account_id, cn_account_id)

    def test_build_rejects_requested_scope_that_is_not_a_positive_ledger_holding(self) -> None:
        cutoff = datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc)
        account_id = self._seed_position_with_market_bar(
            account_name="US Account",
            market="us",
            symbol="AAPL",
            currency="USD",
            source="YfinanceFetcher|adjustment=adjusted",
            cutoff=cutoff,
        )

        with self.assertRaisesRegex(ValueError, "research scope contains non-held positions"):
            self._service().build(
                cutoff=cutoff,
                scope=[{"account_id": account_id, "market": "us", "symbol": "MSFT"}],
            )

    def test_build_freezes_prepared_product_evidence_without_registry_write(self) -> None:
        cutoff = datetime(2026, 8, 6, 21, 0, tzinfo=timezone.utc)
        account_id = self._seed_position_with_market_bar(
            account_name="US Account",
            market="us",
            symbol="PTIR",
            currency="USD",
            source="YfinanceFetcher|adjustment=adjusted",
            cutoff=cutoff,
            bar_date=date(2026, 8, 6),
        )
        self._seed_strategy_benchmark_bar(
            code="SPY",
            source="YfinanceFetcher|adjustment=adjusted",
            cutoff=cutoff,
            bar_date=date(2026, 8, 6),
        )
        self._seed_risk_policy()
        with self.db.get_session() as session:
            instrument = session.execute(
                select(PortfolioInstrument).where(
                    PortfolioInstrument.market == "us",
                    PortfolioInstrument.symbol == "PTIR",
                )
            ).scalar_one()
            instrument.instrument_type = "daily_leveraged_product"
            instrument.underlying_symbol = "PLTR"
            instrument.underlying_market = "us"
            instrument.underlying_currency = "USD"
            instrument.leverage_factor = 2.0
            instrument.daily_reset = True
            instrument.metadata_json = json.dumps(
                {"name": "GraniteShares 2x Long PLTR Daily ETF"}
            )
            session.commit()

        def component(**values):
            return build_product_evidence_component(
                as_of=cutoff,
                source="verified-fixture",
                source_version="v1",
                **values,
            )
        raw_product_evidence = {
            "schema_version": PRODUCT_EVIDENCE_SCHEMA_VERSION,
            "instrument_type": "daily_leveraged_product",
            "market": "us",
            "symbol": "PTIR",
            "evidence_cutoff": cutoff.isoformat(),
            "official_terms": component(
                terms_url="https://graniteshares.com/etfs/ptir/",
                daily_reset=True,
                leverage_factor=2.0,
            ),
            "underlying_same_cutoff": component(
                market="us",
                symbol="PLTR",
                currency="USD",
                completed_session=True,
            ),
            "intraday_leverage": component(
                leverage_factor=2.0,
                product_return_pct=1.8,
                underlying_return_pct=1.0,
                observed_leverage=1.8,
            ),
            "path_decay_rebalance": component(
                path_dependency_disclosed=True,
                rebalance_frequency="daily",
            ),
            "liquidity": component(spread_bps=8.0),
            "horizon_fit": component(evaluated=True, fits_holding_period=False),
        }
        normalized = product_evidence_from_instrument(
            {
                "market": "us",
                "symbol": "PTIR",
                "instrument_type": "daily_leveraged_product",
                "underlying_symbol": "PLTR",
                "underlying_market": "us",
                "underlying_currency": "USD",
                "leverage_factor": 2.0,
                "daily_reset": True,
                "verification_status": "verified",
                "product_evidence": raw_product_evidence,
            },
            cutoff=cutoff,
        )
        assert normalized is not None
        prepared_product_evidence = {
            key: value for key, value in normalized.items() if key != "blockers"
        }

        snapshot = self._service().build(
            cutoff=cutoff,
            scope=[{"account_id": account_id, "market": "us", "symbol": "PTIR"}],
            prepared_product_evidence_items=[
                {
                    "account_id": account_id,
                    "market": "us",
                    "symbol": "PTIR",
                    "product_evidence": prepared_product_evidence,
                }
            ],
        )

        frozen = snapshot["instruments"][0]
        self.assertEqual(frozen["product_evidence"]["status"], "ready")
        self.assertEqual(
            frozen["product_evidence_by_account"][str(account_id)]["evidence_hash"],
            prepared_product_evidence["evidence_hash"],
        )
        with self.db.get_session() as session:
            instrument = session.execute(
                select(PortfolioInstrument).where(
                    PortfolioInstrument.market == "us",
                    PortfolioInstrument.symbol == "PTIR",
                )
            ).scalar_one()
            self.assertNotIn("product_evidence", json.loads(instrument.metadata_json))

    def _seed_active_signal(
        self,
        *,
        account_id: int,
        created_at: datetime,
        updated_at: datetime,
        position_action: str = "hold",
        extra_metadata: dict | None = None,
    ) -> int:
        metadata = {
            "quality_context_status": "complete",
            "portfolio_decision": {
                "account_id": account_id,
                "market": "us",
                "stock_code": "AAPL",
                "position_action": position_action,
                "incremental_action": "wait",
                "evidence_cutoff": "2026-07-22T08:00:00Z",
            },
            **(extra_metadata or {}),
        }
        with self.db.get_session() as session:
            row = DecisionSignalRecord(
                stock_code="AAPL",
                stock_name="Apple",
                market="us",
                source_type="analysis",
                trigger_source="portfolio",
                action="hold",
                reason="Frozen reason",
                status="active",
                created_at=created_at,
                updated_at=updated_at,
                metadata_json=json.dumps(metadata),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return int(row.id)

    def _seed_evidence_sidecar(
        self,
        *,
        signal_id: int,
        readiness_status: str = "complete",
    ) -> None:
        snapshot = DecisionEvidenceSnapshot.model_validate(
            {
                "schema_version": "decision-evidence-snapshot-v1",
                "signal_id": signal_id,
                "quality_context_id": None,
                "strategy_key": "portfolio-current-policy",
                "strategy_version": "1.0.0",
                "strategy_manifest_hash": "a" * 64,
                "decision_cutoff": "2026-07-22T08:00:00Z",
                "reporting_currency": "USD",
                "structured_inputs": {"account_id": 1, "market": "us"},
                "evidence_bundle": {"benchmark": {"code": "SPY"}},
                "readiness_status": readiness_status,
                "blockers": (
                    [] if readiness_status == "complete" else ["benchmark_bar_missing"]
                ),
                "snapshot_hash": "b" * 64,
            }
        )
        sidecar, _ = DecisionEvidenceSnapshotRepository(self.db).create_if_absent(
            snapshot.to_record_fields()
        )
        with self.db.get_session() as session:
            row = session.get(DecisionSignalRecord, signal_id)
            metadata = json.loads(row.metadata_json)
            metadata.update(
                {
                    "decision_evidence_snapshot_id": sidecar.id,
                    "decision_evidence_research_snapshot_hash": sidecar.snapshot_hash,
                    "decision_evidence_bundle_hash": sidecar.evidence_bundle_hash,
                    "decision_evidence_input_hash": sidecar.decision_input_hash,
                }
            )
            row.metadata_json = json.dumps(metadata)
            session.commit()

    def _seed_cached_position(self) -> int:
        account = self.repo.create_account(
            name="Private Account Name",
            broker="Private Broker",
            market="us",
            base_currency="USD",
            owner_id="private-owner",
        )
        self.repo.replace_positions_lots_and_snapshot(
            account_id=account.id,
            snapshot_date=date(2026, 7, 22),
            cost_method="fifo",
            base_currency="USD",
            total_cash=5000,
            total_market_value=1100,
            total_equity=6100,
            unrealized_pnl=100,
            realized_pnl=0,
            fee_total=0,
            tax_total=0,
            fx_stale=False,
            payload="{}",
            positions=[
                {
                    "symbol": "AAPL",
                    "market": "us",
                    "currency": "USD",
                    "quantity": 10,
                    "avg_cost": 100,
                    "total_cost": 1000,
                    "last_price": 110,
                    "market_value_base": 1100,
                    "unrealized_pnl_base": 100,
                }
            ],
            lots=[],
            valuation_currency="USD",
        )
        self._seed_market_evidence_batch(
            code="AAPL",
            close=110.0,
            source="YfinanceFetcher",
            adjustment="adjusted",
            cutoff=datetime(2026, 7, 23, 8, 0, 0, tzinfo=timezone.utc),
        )
        return account.id

    def _seed_position_with_market_bar(
        self,
        *,
        account_name: str,
        market: str,
        symbol: str,
        currency: str,
        source: str,
        cutoff: datetime,
        bar_date: date | None = None,
    ) -> int:
        account = self.repo.create_account(
            name=account_name,
            broker="Test Broker",
            market=market,
            base_currency=currency,
        )
        snapshot_date = (cutoff - timedelta(days=1)).date()
        self.repo.replace_positions_lots_and_snapshot(
            account_id=account.id,
            snapshot_date=snapshot_date,
            cost_method="fifo",
            base_currency=currency,
            total_cash=5000,
            total_market_value=1000,
            total_equity=6000,
            unrealized_pnl=0,
            realized_pnl=0,
            fee_total=0,
            tax_total=0,
            fx_stale=False,
            payload="{}",
            positions=[
                {
                    "symbol": symbol,
                    "market": market,
                    "currency": currency,
                    "quantity": 10,
                    "avg_cost": 90,
                    "total_cost": 900,
                    "last_price": 100,
                    "market_value_base": 1000,
                    "unrealized_pnl_base": 100,
                }
            ],
            lots=[],
            valuation_currency=currency,
        )
        source_name, _, adjustment = source.partition("|adjustment=")
        self._seed_market_evidence_batch(
            code=symbol,
            close=100.0,
            source=source_name,
            adjustment=adjustment or "unknown",
            cutoff=cutoff,
            bar_date=bar_date,
        )
        PortfolioInstrumentService(repo=self.repo).create_instrument(
            {
                "symbol": symbol,
                "market": market,
                "quote_currency": currency,
                "instrument_type": "equity",
                "trade_lot_size": 1,
                "verification_status": "verified",
                "evidence_source": "test-registry",
                "evidence_as_of": cutoff - timedelta(days=1),
            }
        )
        return account.id

    def _seed_strategy_benchmark_bar(
        self,
        *,
        code: str,
        source: str,
        cutoff: datetime,
        bar_date: date | None = None,
    ) -> None:
        source_name, _, adjustment = source.partition("|adjustment=")
        self._seed_market_evidence_batch(
            code=code,
            close=100.0,
            source=source_name,
            adjustment=adjustment or "unknown",
            cutoff=cutoff,
            bar_date=bar_date,
        )

    def _seed_market_evidence_batch(
        self,
        *,
        code: str,
        close: float,
        source: str,
        adjustment: str,
        cutoff: datetime,
        captured_at: datetime | None = None,
        bar_date: date | None = None,
    ):
        return PortfolioMarketEvidenceRepository(self.db).append_batch(
            pd.DataFrame(
                [
                    {
                        "date": bar_date or (cutoff - timedelta(days=1)).date(),
                        "open": close,
                        "high": close,
                        "low": close,
                        "close": close,
                        "volume": 1.0,
                        "amount": close,
                        "pct_chg": 0.0,
                    }
                ]
            ),
            code=code,
            data_source=source,
            source_version="portfolio-research-evidence-prepare-v2",
            adjustment_identity=adjustment,
            captured_at=captured_at or cutoff - timedelta(hours=1),
        )

    def _seed_risk_policy(self) -> None:
        PortfolioRiskPolicyService(repo=self.repo).save_policy(
            {
                "min_cash_buffer_pct": 10,
                "max_single_position_pct": 25,
                "max_sector_pct": 40,
                "max_high_risk_product_pct": 5,
                "max_portfolio_drawdown_pct": 15,
            }
        )

    def _seed_control_plane(self) -> None:
        PortfolioInstrumentService(repo=self.repo).create_instrument(
            {
                "symbol": "AAPL",
                "market": "us",
                "quote_currency": "USD",
                "instrument_type": "equity",
                "trade_lot_size": 1,
                "verification_status": "verified",
                "evidence_source": "NASDAQ symbol directory",
                "evidence_as_of": datetime(2026, 7, 22, 7, 0, 0, tzinfo=timezone.utc),
                "metadata": {
                    "name": "Apple Inc.",
                    "private_note": "must not leave DSA snapshot",
                    "risk_sector": {
                        "taxonomy": "portfolio-risk-v1",
                        "as_of": "2026-07-22",
                        "source": "https://investor.apple.com/",
                        "exposures": [
                            {"sector": "Technology Hardware", "weight_pct": 100},
                        ],
                    },
                },
            }
        )
        PortfolioRiskPolicyService(repo=self.repo).save_policy(
            {
                "min_cash_buffer_pct": 10,
                "max_single_position_pct": 25,
                "max_sector_pct": 40,
                "max_high_risk_product_pct": 5,
                "max_portfolio_drawdown_pct": 15,
            }
        )

    def _seed_prior_daily_snapshot(
        self,
        account_id: int,
        *,
        total_equity: float = 6500,
    ) -> None:
        with self.db.get_session() as session:
            session.add(
                PortfolioDailySnapshot(
                    account_id=account_id,
                    snapshot_date=date(2026, 7, 21),
                    cost_method="fifo",
                    base_currency="USD",
                    total_cash=5000,
                    total_market_value=total_equity - 5000,
                    total_equity=total_equity,
                    unrealized_pnl=0,
                    realized_pnl=0,
                    fee_total=0,
                    tax_total=0,
                    fx_stale=False,
                    payload="{}",
                )
            )
            session.commit()

    def _mutable_row_counts(self) -> dict[str, int]:
        models = (
            PortfolioPosition,
            PortfolioDailySnapshot,
            DecisionSignalRecord,
            DecisionSignalOutcomeRecord,
        )
        with self.db.get_session() as session:
            return {
                model.__tablename__: session.execute(
                    select(func.count()).select_from(model)
                ).scalar_one()
                for model in models
            }

    def test_snapshot_is_canonical_hashed_and_excludes_private_account_fields(self) -> None:
        self._seed_cached_position()
        self._seed_control_plane()
        PortfolioInstrumentService(repo=self.repo).create_instrument(
            {
                "symbol": "MSFT",
                "market": "us",
                "quote_currency": "USD",
                "instrument_type": "equity",
                "trade_lot_size": 1,
                "verification_status": "provisional",
            }
        )
        cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)

        first = self._service().build(cutoff=cutoff)
        second = self._service().build(cutoff=cutoff)

        self.assertEqual(first["snapshot_hash"], second["snapshot_hash"])
        self.assertEqual(first["universe_hash"], second["universe_hash"])
        self.assertEqual(first["positions"][0]["symbol"], "AAPL")
        self.assertNotIn("avg_cost", first["positions"][0])
        self.assertNotIn("total_cost", first["positions"][0])
        self.assertNotIn("unrealized_pnl_base", first["positions"][0])
        self.assertEqual(
            [item["symbol"] for item in first["instruments"]],
            ["AAPL"],
        )
        aapl = next(item for item in first["instruments"] if item["symbol"] == "AAPL")
        self.assertEqual(aapl["evidence_as_of"], "2026-07-22T07:00:00+00:00")
        self.assertIn(
            "decision_price_stale",
            {item["code"] for item in first["hard_blockers"]},
        )
        self.assertIn(
            "portfolio_risk_budget_thresholds_not_evaluated",
            first["limitations"],
        )
        rendered = str(first)
        self.assertNotIn("private-owner", rendered)
        self.assertNotIn("Private Broker", rendered)
        self.assertNotIn("Private Account Name", rendered)
        self.assertNotIn("private_note", rendered)

    def test_snapshot_emits_complete_source_metadata_from_prepared_bars(self) -> None:
        self._seed_cached_position()
        self._seed_control_plane()
        cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)
        position_batch = self._seed_market_evidence_batch(
            code="AAPL",
            close=110.0,
            source="YfinanceFetcher",
            adjustment="adjusted",
            cutoff=cutoff,
            bar_date=date(2026, 7, 31),
        )
        benchmark_batch = self._seed_market_evidence_batch(
            code="SPY",
            close=620.0,
            source="YfinanceFetcher",
            adjustment="adjusted",
            cutoff=cutoff,
            bar_date=date(2026, 7, 31),
        )

        snapshot = self._service().build(cutoff=cutoff)

        position = snapshot["positions"][0]
        account = snapshot["accounts"][0]
        instrument = next(
            item for item in snapshot["instruments"] if item["symbol"] == "AAPL"
        )
        benchmark = snapshot["benchmarks"][0]
        for item, fields in (
            (account, ("evidence_source", "evidence_version", "evidence_hash")),
            (position, ("price_source", "price_source_version", "price_source_hash")),
            (instrument, ("evidence_version", "evidence_hash", "adjustment_identity")),
            (benchmark, ("evidence_as_of", "evidence_hash", "adjustment_identity")),
            (snapshot["risk_policy"], ("evidence_source", "evidence_version", "evidence_hash")),
            (snapshot["risk_budget"], ("as_of", "evidence_source", "evidence_version", "evidence_hash")),
        ):
            self.assertTrue(all(item.get(field) for field in fields), (item, fields))
        self.assertEqual(position["last_price"], 110.0)
        self.assertEqual(benchmark["price"], 620.0)
        self.assertEqual(position["adjustment_identity"], "adjusted")
        self.assertEqual(position["price_evidence_batch_hash"], position_batch.batch_hash)
        self.assertEqual(benchmark["evidence_batch_hash"], benchmark_batch.batch_hash)
        self.assertEqual(instrument["name"], "Apple Inc.")

    def test_snapshot_blocks_cn_position_benchmark_adjustment_mismatch(self) -> None:
        account_id = self._seed_position_with_market_bar(
            account_name="CN Account",
            market="cn",
            symbol="600519",
            currency="CNY",
            source="TencentFetcher|adjustment=qfq",
            cutoff=TEST_RESEARCH_CUTOFF,
        )
        self._seed_strategy_benchmark_bar(
            code="000300",
            source="YfinanceFetcher|adjustment=adjusted",
            cutoff=TEST_RESEARCH_CUTOFF,
        )
        self._seed_risk_policy()

        snapshot = self._service().build(cutoff=TEST_RESEARCH_CUTOFF)

        mismatches = [
            item
            for item in snapshot["hard_blockers"]
            if item["code"] == "benchmark_adjustment_identity_mismatch"
        ]
        self.assertEqual(
            mismatches,
            [
                {
                    "code": "benchmark_adjustment_identity_mismatch",
                    "scope": "position",
                    "account_id": account_id,
                    "market": "cn",
                    "symbol": "600519",
                    "benchmark_symbol": "000300",
                }
            ],
        )
        self.assertEqual(snapshot["completeness"], "INSUFFICIENT_EVIDENCE")

    def test_snapshot_accepts_matching_position_benchmark_adjustments(self) -> None:
        cases = (
            ("cn", "600519", "CNY", "000300", "TencentFetcher|adjustment=qfq"),
            ("us", "AAPL", "USD", "SPY", "YfinanceFetcher|adjustment=adjusted"),
        )
        account_ids = []
        for market, symbol, currency, benchmark, source in cases:
            expected_bar_date = get_effective_trading_date(
                market,
                current_time=TEST_RESEARCH_CUTOFF,
            )
            account_ids.append(
                self._seed_position_with_market_bar(
                    account_name=f"{market.upper()} Account",
                    market=market,
                    symbol=symbol,
                    currency=currency,
                    source=source,
                    cutoff=TEST_RESEARCH_CUTOFF,
                    bar_date=expected_bar_date,
                )
            )
            self._seed_strategy_benchmark_bar(
                code=benchmark,
                source=source,
                cutoff=TEST_RESEARCH_CUTOFF,
                bar_date=expected_bar_date,
            )
        self._seed_risk_policy()
        frozen_at = TEST_RESEARCH_CUTOFF - timedelta(hours=1)
        with self.db.get_session() as session:
            session.execute(
                update(PortfolioAccount).values(
                    created_at=frozen_at,
                    updated_at=frozen_at,
                )
            )
            session.execute(
                update(PortfolioDailySnapshot).values(updated_at=frozen_at)
            )
            session.execute(update(PortfolioPosition).values(updated_at=frozen_at))
            session.execute(
                update(PortfolioInstrument).values(
                    created_at=frozen_at,
                    updated_at=frozen_at,
                )
            )
            session.execute(
                update(PortfolioRiskPolicy).values(
                    created_at=frozen_at,
                    updated_at=frozen_at,
                )
            )
            session.commit()

        snapshot = self._service().build(cutoff=TEST_RESEARCH_CUTOFF)

        adjustment_blockers = [
            item
            for item in snapshot["hard_blockers"]
            if "adjustment_identity" in item["code"]
        ]
        self.assertEqual(adjustment_blockers, [])
        self.assertTrue(
            all(
                not item["price_evidence_not_final"]
                and not item["price_evidence_stale"]
                for item in snapshot["positions"]
            ),
            snapshot["positions"],
        )
        self.assertTrue(
            all(not item["not_final"] and not item["stale"] for item in snapshot["benchmarks"])
        )
        self.assertEqual(
            {item["account_id"] for item in snapshot["positions"]},
            set(account_ids),
        )
        self.assertEqual(snapshot["completeness"], "COMPLETE", snapshot["hard_blockers"])

    def test_snapshot_blocks_missing_position_adjustment_identity(self) -> None:
        account_id = self._seed_position_with_market_bar(
            account_name="CN Account",
            market="cn",
            symbol="600519",
            currency="CNY",
            source="TencentFetcher",
            cutoff=TEST_RESEARCH_CUTOFF,
        )
        self._seed_strategy_benchmark_bar(
            code="000300",
            source="TencentFetcher|adjustment=qfq",
            cutoff=TEST_RESEARCH_CUTOFF,
        )
        self._seed_risk_policy()

        snapshot = self._service().build(cutoff=TEST_RESEARCH_CUTOFF)

        adjustment_blockers = [
            item
            for item in snapshot["hard_blockers"]
            if "adjustment_identity" in item["code"]
        ]
        self.assertEqual(
            adjustment_blockers,
            [
                {
                    "code": "position_adjustment_identity_unknown",
                    "scope": "position",
                    "account_id": account_id,
                    "market": "cn",
                    "symbol": "600519",
                    "benchmark_symbol": "000300",
                }
            ],
        )
        self.assertEqual(snapshot["completeness"], "INSUFFICIENT_EVIDENCE")

    def test_snapshot_blocks_missing_benchmark_adjustment_identity(self) -> None:
        account_id = self._seed_position_with_market_bar(
            account_name="CN Account",
            market="cn",
            symbol="600519",
            currency="CNY",
            source="TencentFetcher|adjustment=qfq",
            cutoff=TEST_RESEARCH_CUTOFF,
        )
        self._seed_strategy_benchmark_bar(
            code="000300",
            source="TencentFetcher",
            cutoff=TEST_RESEARCH_CUTOFF,
        )
        self._seed_risk_policy()

        snapshot = self._service().build(cutoff=TEST_RESEARCH_CUTOFF)

        adjustment_blockers = [
            item
            for item in snapshot["hard_blockers"]
            if "adjustment_identity" in item["code"]
        ]
        self.assertEqual(
            adjustment_blockers,
            [
                {
                    "code": "benchmark_adjustment_identity_unknown",
                    "scope": "position",
                    "account_id": account_id,
                    "market": "cn",
                    "symbol": "600519",
                    "benchmark_symbol": "000300",
                }
            ],
        )
        self.assertEqual(snapshot["completeness"], "INSUFFICIENT_EVIDENCE")

    def test_snapshot_adjustment_mismatch_is_position_specific_and_deterministic(self) -> None:
        matching_account_id = self._seed_position_with_market_bar(
            account_name="Matching CN Account",
            market="cn",
            symbol="600519",
            currency="CNY",
            source="TencentFetcher|adjustment=qfq",
            cutoff=TEST_RESEARCH_CUTOFF,
        )
        mismatching_account_id = self._seed_position_with_market_bar(
            account_name="Mismatching CN Account",
            market="cn",
            symbol="000001",
            currency="CNY",
            source="YfinanceFetcher|adjustment=adjusted",
            cutoff=TEST_RESEARCH_CUTOFF,
        )
        us_account_id = self._seed_position_with_market_bar(
            account_name="US Account",
            market="us",
            symbol="AAPL",
            currency="USD",
            source="YfinanceFetcher|adjustment=adjusted",
            cutoff=TEST_RESEARCH_CUTOFF,
        )
        self._seed_strategy_benchmark_bar(
            code="000300",
            source="TencentFetcher|adjustment=qfq",
            cutoff=TEST_RESEARCH_CUTOFF,
        )
        self._seed_strategy_benchmark_bar(
            code="SPY",
            source="YfinanceFetcher|adjustment=adjusted",
            cutoff=TEST_RESEARCH_CUTOFF,
        )
        self._seed_risk_policy()
        cutoff = TEST_RESEARCH_CUTOFF

        first = self._service().build(cutoff=cutoff)
        second = self._service().build(cutoff=cutoff)

        mismatches = [
            item
            for item in first["hard_blockers"]
            if item["code"] == "benchmark_adjustment_identity_mismatch"
        ]
        self.assertEqual(
            mismatches,
            [
                {
                    "code": "benchmark_adjustment_identity_mismatch",
                    "scope": "position",
                    "account_id": mismatching_account_id,
                    "market": "cn",
                    "symbol": "000001",
                    "benchmark_symbol": "000300",
                }
            ],
        )
        self.assertNotIn(matching_account_id, {item["account_id"] for item in mismatches})
        self.assertNotIn(us_account_id, {item["account_id"] for item in mismatches})
        self.assertEqual(first["snapshot_hash"], second["snapshot_hash"])

    def test_prepared_snapshot_can_freeze_complete_decision_evidence(self) -> None:
        account_id = self._seed_cached_position()
        self._seed_control_plane()
        self._seed_prior_daily_snapshot(account_id)
        latest_finalized_day = date(2026, 7, 31)
        cutoff = datetime(2026, 8, 3, 19, 0, 0, tzinfo=timezone.utc)
        frozen_at = cutoff - timedelta(hours=1)
        with self.db.get_session() as session:
            session.execute(
                update(PortfolioAccount).values(
                    created_at=frozen_at,
                    updated_at=frozen_at,
                )
            )
            session.execute(
                update(PortfolioDailySnapshot).values(updated_at=frozen_at)
            )
            session.execute(update(PortfolioPosition).values(updated_at=frozen_at))
            session.execute(
                update(PortfolioInstrument).values(
                    created_at=frozen_at,
                    updated_at=frozen_at,
                )
            )
            session.execute(
                update(PortfolioRiskPolicy).values(
                    created_at=frozen_at,
                    updated_at=frozen_at,
                )
            )
            session.commit()
        for code, close in (("AAPL", 210.0), ("SPY", 620.0)):
            PortfolioMarketEvidenceRepository(self.db).append_batch(
                pd.DataFrame(
                    [{
                        "date": latest_finalized_day,
                        "open": close,
                        "high": close,
                        "low": close,
                        "close": close,
                        "volume": 1.0,
                        "amount": close,
                        "pct_chg": 0.0,
                    }]
                ),
                code=code,
                data_source="YfinanceFetcher",
                source_version="portfolio-research-evidence-prepare-v2",
                adjustment_identity="adjusted",
                captured_at=cutoff - timedelta(seconds=1),
            )
        snapshot = self._service().build(cutoff=cutoff)
        StrategyRegistryService(self.db).create_version(load_strategy_manifest())

        result = DecisionEvidenceSnapshotService(db_manager=self.db).freeze(
            signal={
                "id": 101,
                "market": "us",
                "stock_code": "AAPL",
                "created_at": snapshot["cutoff"],
            },
            portfolio_decision={
                "account_id": account_id,
                "position_action": "hold",
                "incremental_action": "wait",
                "confidence_by_horizon": {"5d": 0.5, "20d": 0.5, "60d": 0.5},
                "supporting_evidence": ["盈利保持稳定"],
                "opposing_evidence": ["估值仍高"],
                "watch_conditions": ["等待下一次财报"],
                "invalidation": "盈利趋势反转",
                "next_review": "下一次财报后",
            },
            research_snapshot=snapshot,
            portfolio_context={"account_id": account_id},
            context_snapshot={
                "decision_evidence": [
                    {
                        "schema_version": "decision-source-envelope-v1",
                        "as_of": snapshot["cutoff"],
                        "source": "frozen-analysis",
                        "source_version": "analysis-v1",
                        "source_hash": "7" * 64,
                        "body": {"summary": "冻结研究证据"},
                    }
                ]
            },
        )

        self.assertEqual(result["status"], "complete", result["unable_reasons"])

    def test_snapshot_marks_stale_strategy_benchmark_as_blocking(self) -> None:
        self._seed_cached_position()
        self._seed_control_plane()
        cutoff = datetime(2026, 7, 31, 8, 0, 0, tzinfo=timezone.utc)
        self._seed_market_evidence_batch(
            code="SPY",
            close=620.0,
            source="YfinanceFetcher",
            adjustment="adjusted",
            cutoff=datetime(2026, 7, 23, 8, 0, 0, tzinfo=timezone.utc),
            captured_at=cutoff - timedelta(hours=1),
        )

        snapshot = self._service().build(cutoff=cutoff)

        self.assertTrue(snapshot["benchmarks"][0]["stale"])
        self.assertIn(
            "benchmark_price_stale",
            {item["code"] for item in snapshot["hard_blockers"]},
        )

    def test_snapshot_rejects_previous_hk_session_after_current_session_close(self) -> None:
        cutoff = datetime(2026, 8, 6, 11, 3, 0, tzinfo=timezone.utc)
        previous_session = date(2026, 8, 5)
        account_id = self._seed_position_with_market_bar(
            account_name="HK Account",
            market="hk",
            symbol="HK07709",
            currency="HKD",
            source="YfinanceFetcher|adjustment=adjusted",
            cutoff=cutoff,
            bar_date=previous_session,
        )
        self._seed_strategy_benchmark_bar(
            code="HSI",
            source="YfinanceFetcher|adjustment=adjusted",
            cutoff=cutoff,
            bar_date=previous_session,
        )
        self._seed_risk_policy()

        snapshot = self._service().build(cutoff=cutoff)
        position = next(item for item in snapshot["positions"] if item["account_id"] == account_id)
        benchmark = next(item for item in snapshot["benchmarks"] if item["market"] == "hk")
        blockers = {item["code"] for item in snapshot["hard_blockers"]}

        self.assertTrue(position["price_evidence_stale"])
        self.assertTrue(benchmark["stale"])
        self.assertIn("decision_price_stale", blockers)
        self.assertIn("benchmark_price_stale", blockers)

    def test_us_friday_close_is_fresh_during_monday_premarket(self) -> None:
        self._seed_cached_position()
        self._seed_control_plane()
        cutoff = datetime(2026, 8, 3, 12, 45, 0, tzinfo=timezone.utc)
        friday = date(2026, 7, 31)
        evidence_repo = PortfolioMarketEvidenceRepository(self.db)
        for code, close in (("AAPL", 210.0), ("SPY", 620.0)):
            evidence_repo.append_batch(
                pd.DataFrame(
                    [{
                        "date": friday,
                        "open": close,
                        "high": close,
                        "low": close,
                        "close": close,
                        "volume": 1.0,
                        "amount": close,
                        "pct_chg": 0.0,
                    }]
                ),
                code=code,
                data_source="YfinanceFetcher",
                source_version="portfolio-research-evidence-prepare-v2",
                adjustment_identity="adjusted",
                captured_at=cutoff - timedelta(hours=1),
            )

        snapshot = self._service().build(cutoff=cutoff)
        blockers = {item["code"] for item in snapshot["hard_blockers"]}

        self.assertEqual(
            snapshot["positions"][0]["price_as_of"],
            "2026-07-31T20:00:00Z",
        )
        self.assertEqual(
            snapshot["benchmarks"][0]["evidence_as_of"],
            "2026-07-31T20:00:00Z",
        )
        self.assertFalse(snapshot["positions"][0]["price_evidence_stale"])
        self.assertFalse(snapshot["benchmarks"][0]["stale"])
        self.assertNotIn("decision_price_stale", blockers)
        self.assertNotIn("benchmark_price_stale", blockers)

    def test_completed_same_day_daily_bar_is_final_after_market_close(self) -> None:
        service = self._service()
        cases = (
            ("cn", "2026-08-06T07:00:00Z", "2026-08-06T11:03:00Z"),
            ("hk", "2026-08-06T08:00:00Z", "2026-08-06T11:03:00Z"),
            ("us", "2026-08-06T20:00:00Z", "2026-08-06T21:00:00Z"),
        )

        for market, bar_as_of, cutoff in cases:
            with self.subTest(market=market):
                self.assertFalse(
                    service._daily_bar_not_final(
                        market=market,
                        as_of=datetime.fromisoformat(bar_as_of.replace("Z", "+00:00")),
                        cutoff=datetime.fromisoformat(cutoff.replace("Z", "+00:00")),
                    )
                )

    def test_same_day_daily_bar_is_not_final_before_market_close(self) -> None:
        service = self._service()
        cases = (
            ("cn", "2026-08-06T07:00:00Z", "2026-08-06T06:00:00Z"),
            ("hk", "2026-08-06T08:00:00Z", "2026-08-06T07:00:00Z"),
            ("us", "2026-08-06T20:00:00Z", "2026-08-06T18:00:00Z"),
        )

        for market, bar_as_of, cutoff in cases:
            with self.subTest(market=market):
                self.assertTrue(
                    service._daily_bar_not_final(
                        market=market,
                        as_of=datetime.fromisoformat(bar_as_of.replace("Z", "+00:00")),
                        cutoff=datetime.fromisoformat(cutoff.replace("Z", "+00:00")),
                    )
                )

    def test_snapshot_recomputes_fx_staleness_at_cutoff(self) -> None:
        self._seed_cached_position()
        cutoff = datetime(2026, 7, 31, 8, 0, 0, tzinfo=timezone.utc)
        self.repo.save_fx_rate(
            from_currency="USD",
            to_currency="CNY",
            rate_date=date(2026, 7, 23),
            rate=7.2,
            source="test-fx@1",
            is_stale=False,
        )
        position = self.repo.list_cached_positions(cost_method="fifo")[0]
        position.valuation_currency = "CNY"
        blockers: list[dict] = []

        payload = self._service()._position_payload(
            position,
            price_bar=StockRepository(self.db).get_start_daily(
                code="AAPL",
                analysis_date=cutoff.date(),
            ),
            cutoff=cutoff.replace(tzinfo=None),
            blockers=blockers,
        )

        self.assertTrue(payload["fx"]["stale"])
        self.assertIn("fx_rate_stale", {item["code"] for item in blockers})

    def test_point_in_time_contract_marks_current_capture_prospective_only(self) -> None:
        self._seed_cached_position()
        self._seed_control_plane()
        cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)

        snapshot = self._service().build(cutoff=cutoff)

        eligibility = snapshot["point_in_time"]
        self.assertEqual(eligibility["scope"], "current_prospective")
        self.assertTrue(eligibility["prospective_decision_eligible"])
        self.assertFalse(eligibility["historical_replay_eligible"])
        self.assertEqual(eligibility["blockers"], [])
        self.assertEqual(
            set(eligibility["source_cutoffs"]),
            {
                "accounts",
                "position_cache",
                "daily_snapshots",
                "instrument_registry",
                "risk_policy",
                "decision_signals",
            },
        )

    def test_point_in_time_contract_blocks_sources_updated_after_cutoff(self) -> None:
        self._seed_cached_position()
        self._seed_control_plane()

        snapshot = self._service().build(
            cutoff=datetime(2026, 7, 22, 9, 0, 0, tzinfo=timezone.utc),
        )

        eligibility = snapshot["point_in_time"]
        self.assertFalse(eligibility["prospective_decision_eligible"])
        self.assertFalse(eligibility["historical_replay_eligible"])
        self.assertEqual(
            eligibility["blockers"],
            [
                "account_state_after_cutoff",
                "daily_snapshot_after_cutoff",
                "instrument_registry_after_cutoff",
                "position_cache_after_cutoff",
                "risk_policy_after_cutoff",
            ],
        )

    def test_execution_identity_ignores_market_cache_fields_but_detects_protected_truth(self) -> None:
        self._seed_cached_position()
        self._seed_control_plane()
        service = self._service()
        snapshot = service.build(
            cutoff=datetime(2026, 7, 22, 9, 0, 0, tzinfo=timezone.utc),
        )

        market_refresh = json.loads(json.dumps(snapshot))
        market_refresh["positions"][0]["last_price"] = 999.0
        market_refresh["positions"][0]["cache_updated_at"] = "2026-07-22T10:00:00"
        market_refresh["accounts"][0]["total_equity"] = 999999.0
        market_refresh["point_in_time"]["blockers"] = ["position_cache_after_cutoff"]
        market_refresh["decision_signals"] = [{"id": 999}]

        self.assertEqual(
            type(service)._execution_identity_hash(market_refresh),
            snapshot["execution_identity_hash"],
        )

        changed_holding = json.loads(json.dumps(snapshot))
        changed_holding["positions"][0]["quantity"] += 1
        self.assertNotEqual(
            type(service)._execution_identity_hash(changed_holding),
            snapshot["execution_identity_hash"],
        )

    def test_frozen_active_signal_is_public_and_changes_snapshot_hash(self) -> None:
        account_id = self._seed_cached_position()
        self._seed_control_plane()
        signal_id = self._seed_active_signal(
            account_id=account_id,
            created_at=datetime(2026, 7, 22, 7, 30, 0),
            updated_at=datetime(2026, 7, 22, 8, 0, 0),
            extra_metadata={"private_note": "must not leave snapshot"},
        )
        self._seed_evidence_sidecar(signal_id=signal_id)
        cutoff = datetime(2026, 7, 22, 9, 0, 0, tzinfo=timezone.utc)

        first = self._service().build(cutoff=cutoff)
        with self.db.get_session() as session:
            row = session.get(DecisionSignalRecord, signal_id)
            metadata = json.loads(row.metadata_json)
            metadata["portfolio_decision"]["position_action"] = "reduce"
            row.metadata_json = json.dumps(metadata)
            row.updated_at = datetime(2026, 7, 22, 8, 30, 0)
            session.commit()
        second = self._service().build(cutoff=cutoff)

        self.assertNotEqual(first["snapshot_hash"], second["snapshot_hash"])
        frozen = first["decision_signals"]
        self.assertEqual(len(frozen), 1)
        self.assertEqual(
            set(frozen[0]),
            {
                "id",
                "market",
                "stock_code",
                "stock_name",
                "reason",
                "status",
                "created_at",
                "updated_at",
                "metadata",
            },
        )
        self.assertEqual(
            frozen[0]["metadata"]["portfolio_decision"]["position_action"],
            "hold",
        )
        self.assertNotIn("private_note", frozen[0]["metadata"])
        self.assertEqual(
            frozen[0]["metadata"]["decision_evidence"],
            {
                "status": "complete",
                "display_status": "已保存",
                "reference_status": "matched",
                "unable_reasons": [],
            },
        )
        self.assertFalse(
            {
                "snapshot_hash",
                "evidence_bundle_hash",
                "decision_input_hash",
                "evidence_bundle",
                "structured_inputs",
            }
            & set(frozen[0]["metadata"]["decision_evidence"])
        )

    def test_frozen_active_signal_reports_missing_evidence_sidecar(self) -> None:
        account_id = self._seed_cached_position()
        self._seed_control_plane()
        self._seed_active_signal(
            account_id=account_id,
            created_at=datetime(2026, 7, 22, 7, 30, 0),
            updated_at=datetime(2026, 7, 22, 8, 0, 0),
        )

        snapshot = self._service().build(
            cutoff=datetime(2026, 7, 22, 9, 0, 0, tzinfo=timezone.utc),
        )

        assert snapshot["decision_signals"][0]["metadata"]["decision_evidence"] == {
            "status": "missing",
            "display_status": "资料不足",
            "reference_status": "missing",
            "unable_reasons": ["legacy_evidence_snapshot_missing"],
        }

    def test_frozen_active_signal_reports_incomplete_evidence_sidecar(self) -> None:
        account_id = self._seed_cached_position()
        self._seed_control_plane()
        signal_id = self._seed_active_signal(
            account_id=account_id,
            created_at=datetime(2026, 7, 22, 7, 30, 0),
            updated_at=datetime(2026, 7, 22, 8, 0, 0),
        )
        self._seed_evidence_sidecar(
            signal_id=signal_id,
            readiness_status="insufficient_evidence",
        )

        snapshot = self._service().build(
            cutoff=datetime(2026, 7, 22, 9, 0, 0, tzinfo=timezone.utc),
        )

        assert snapshot["decision_signals"][0]["metadata"]["decision_evidence"] == {
            "status": "insufficient_evidence",
            "display_status": "资料不足",
            "reference_status": "matched",
            "unable_reasons": ["benchmark_bar_missing"],
        }

    def test_frozen_active_signal_rejects_mismatched_evidence_reference(self) -> None:
        account_id = self._seed_cached_position()
        self._seed_control_plane()
        signal_id = self._seed_active_signal(
            account_id=account_id,
            created_at=datetime(2026, 7, 22, 7, 30, 0),
            updated_at=datetime(2026, 7, 22, 8, 0, 0),
        )
        self._seed_evidence_sidecar(signal_id=signal_id)
        with self.db.get_session() as session:
            row = session.get(DecisionSignalRecord, signal_id)
            metadata = json.loads(row.metadata_json)
            metadata["decision_evidence_bundle_hash"] = "f" * 64
            row.metadata_json = json.dumps(metadata)
            session.commit()

        snapshot = self._service().build(
            cutoff=datetime(2026, 7, 22, 9, 0, 0, tzinfo=timezone.utc),
        )

        evidence = snapshot["decision_signals"][0]["metadata"]["decision_evidence"]
        assert evidence["status"] == "insufficient_evidence"
        assert evidence["reference_status"] == "mismatch"
        assert evidence["unable_reasons"] == ["decision_evidence_reference_mismatch"]

    def test_frozen_active_signal_treats_non_object_metadata_as_empty(self) -> None:
        account_id = self._seed_cached_position()
        self._seed_control_plane()
        signal_id = self._seed_active_signal(
            account_id=account_id,
            created_at=datetime(2026, 7, 22, 7, 30, 0),
            updated_at=datetime(2026, 7, 22, 8, 0, 0),
        )
        self._seed_evidence_sidecar(signal_id=signal_id)
        with self.db.get_session() as session:
            session.get(DecisionSignalRecord, signal_id).metadata_json = "[]"
            session.commit()

        snapshot = self._service().build(
            cutoff=datetime(2026, 7, 22, 9, 0, 0, tzinfo=timezone.utc),
        )

        assert snapshot["decision_signals"][0]["metadata"]["decision_evidence"] == {
            "status": "insufficient_evidence",
            "display_status": "资料不足",
            "reference_status": "mismatch",
            "unable_reasons": ["decision_evidence_reference_mismatch"],
        }

    def test_point_in_time_blocks_active_signal_after_cutoff(self) -> None:
        account_id = self._seed_cached_position()
        self._seed_control_plane()
        self._seed_active_signal(
            account_id=account_id,
            created_at=datetime(2026, 7, 22, 8, 30, 0),
            updated_at=datetime(2026, 7, 22, 9, 30, 0),
        )

        snapshot = self._service().build(
            cutoff=datetime(2026, 7, 22, 9, 0, 0, tzinfo=timezone.utc),
        )

        self.assertIn(
            "decision_signal_after_cutoff",
            snapshot["point_in_time"]["blockers"],
        )

    def test_frozen_snapshot_excludes_signals_created_after_cutoff(self) -> None:
        account_id = self._seed_cached_position()
        self._seed_control_plane()
        cutoff = datetime(2026, 7, 22, 9, 0, 0, tzinfo=timezone.utc)
        first = self._service().build(cutoff=cutoff)
        self._seed_active_signal(
            account_id=account_id,
            created_at=datetime(2026, 7, 22, 9, 30, 0),
            updated_at=datetime(2026, 7, 22, 9, 30, 0),
        )

        second = self._service().build(cutoff=cutoff)

        self.assertEqual(second["snapshot_hash"], first["snapshot_hash"])
        self.assertEqual(second["decision_signals"], first["decision_signals"])
        self.assertNotIn(
            "decision_signal_after_cutoff",
            second["point_in_time"]["blockers"],
        )

    def test_point_in_time_blocks_present_signal_with_missing_timestamp(self) -> None:
        account_id = self._seed_cached_position()
        self._seed_control_plane()
        signal_id = self._seed_active_signal(
            account_id=account_id,
            created_at=datetime(2026, 7, 22, 7, 30, 0),
            updated_at=datetime(2026, 7, 22, 8, 0, 0),
        )
        with self.db.get_session() as session:
            session.get(DecisionSignalRecord, signal_id).updated_at = None
            session.commit()

        snapshot = self._service().build(
            cutoff=datetime(2026, 7, 22, 9, 0, 0, tzinfo=timezone.utc),
        )

        self.assertIn(
            "decision_signal_cutoff_missing",
            snapshot["point_in_time"]["blockers"],
        )

    def test_snapshot_projects_latest_signal_without_history_truncation(self) -> None:
        account_id = self._seed_cached_position()
        self._seed_control_plane()
        cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)
        latest_signal_id = None
        for index in range(101):
            created_at = cutoff.replace(tzinfo=None) - timedelta(minutes=101 - index)
            latest_signal_id = self._seed_active_signal(
                account_id=account_id,
                created_at=created_at,
                updated_at=created_at,
            )

        snapshot = self._service().build(cutoff=cutoff)

        self.assertEqual(len(snapshot["decision_signals"]), 1)
        self.assertEqual(snapshot["decision_signals"][0]["id"], latest_signal_id)
        self.assertNotIn(
            "decision_signal_snapshot_truncated",
            snapshot["point_in_time"]["blockers"],
        )

    def test_snapshot_projects_latest_account_specific_signal_per_holding(self) -> None:
        first_account_id = self._seed_cached_position()
        second_account_id = self._seed_cached_position()
        self._seed_control_plane()
        cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)
        selected_ids = []
        for account_id in (first_account_id, second_account_id):
            older_at = cutoff.replace(tzinfo=None) - timedelta(minutes=2)
            newer_at = cutoff.replace(tzinfo=None) - timedelta(minutes=1)
            self._seed_active_signal(
                account_id=account_id,
                created_at=older_at,
                updated_at=older_at,
            )
            selected_ids.append(
                self._seed_active_signal(
                    account_id=account_id,
                    created_at=newer_at,
                    updated_at=newer_at,
                )
            )

        snapshot = self._service().build(cutoff=cutoff)

        self.assertEqual(
            {item["id"] for item in snapshot["decision_signals"]},
            set(selected_ids),
        )

    def test_snapshot_hash_changes_when_latest_reference_signal_changes(self) -> None:
        account_id = self._seed_cached_position()
        self._seed_control_plane()
        cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)
        first_at = cutoff.replace(tzinfo=None) - timedelta(minutes=2)
        self._seed_active_signal(
            account_id=account_id,
            created_at=first_at,
            updated_at=first_at,
        )
        first = self._service().build(cutoff=cutoff)
        second_at = cutoff.replace(tzinfo=None) - timedelta(minutes=1)
        latest_signal_id = self._seed_active_signal(
            account_id=account_id,
            created_at=second_at,
            updated_at=second_at,
        )

        second = self._service().build(cutoff=cutoff)

        self.assertNotEqual(first["snapshot_hash"], second["snapshot_hash"])
        self.assertEqual(
            [item["id"] for item in second["decision_signals"]],
            [latest_signal_id],
        )

    def test_point_in_time_blocks_truncated_reference_projection(self) -> None:
        first_account_id = self._seed_cached_position()
        second_account_id = self._seed_cached_position()
        self._seed_control_plane()
        cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)
        created_at = cutoff.replace(tzinfo=None) - timedelta(minutes=1)
        for account_id in (first_account_id, second_account_id):
            self._seed_active_signal(
                account_id=account_id,
                created_at=created_at,
                updated_at=created_at,
            )

        snapshot = self._service(max_decision_signals=1).build(cutoff=cutoff)

        self.assertEqual(len(snapshot["decision_signals"]), 1)
        self.assertIn(
            "decision_signal_snapshot_truncated",
            snapshot["point_in_time"]["blockers"],
        )

    def test_snapshot_reports_missing_identity_and_risk_policy_as_hard_blockers(self) -> None:
        self._seed_cached_position()

        snapshot = self._service().build(cutoff=datetime(2026, 7, 22, 9, 0, 0))

        blocker_codes = {item["code"] for item in snapshot["hard_blockers"]}
        self.assertIn("portfolio_risk_policy_missing", blocker_codes)
        self.assertIn("instrument_identity_missing", blocker_codes)
        self.assertEqual(snapshot["completeness"], "INSUFFICIENT_EVIDENCE")

    def test_snapshot_build_is_read_only(self) -> None:
        self._seed_cached_position()
        self._seed_control_plane()
        before = self._mutable_row_counts()

        self._service().build(cutoff=datetime(2026, 7, 22, 9, 0, 0))

        self.assertEqual(self._mutable_row_counts(), before)

    def test_snapshot_proves_effective_agent_execution_architecture(self) -> None:
        self._seed_cached_position()
        self._seed_control_plane()
        cutoff = datetime(2026, 7, 22, 9, 0, 0)

        with patch.dict(os.environ, {"AGENT_ARCH": "single"}, clear=False):
            Config.reset_instance()
            single_snapshot = self._service().build(cutoff=cutoff)

        with patch.dict(os.environ, {"AGENT_ARCH": "multi"}, clear=False):
            Config.reset_instance()
            multi_snapshot = self._service().build(cutoff=cutoff)

        Config.reset_instance()
        self.assertEqual(
            single_snapshot["analysis_runtime"],
            {"architecture": "single", "automatic_multi_agent": False},
        )
        self.assertEqual(
            multi_snapshot["analysis_runtime"],
            {"architecture": "multi", "automatic_multi_agent": True},
        )

    def test_risk_budget_evaluates_complete_native_currency_scope(self) -> None:
        account_id = self._seed_cached_position()
        self._seed_control_plane()
        self._seed_prior_daily_snapshot(account_id)

        snapshot = self._service().build(cutoff=datetime(2026, 7, 22, 9, 0, 0))

        self.assertTrue(snapshot["risk_budget"]["evaluated"])
        self.assertEqual(snapshot["risk_budget"]["base_scope"], "currency")
        self.assertEqual(snapshot["risk_budget"]["breaches"], [])
        usd_scope = snapshot["risk_budget"]["scopes"][0]
        self.assertEqual(usd_scope["currency"], "USD")
        self.assertAlmostEqual(usd_scope["cash_buffer_pct"], 5000 / 6100 * 100, places=4)
        self.assertAlmostEqual(usd_scope["max_single_position_pct"], 1100 / 6100 * 100, places=4)
        self.assertAlmostEqual(usd_scope["max_sector_pct"], 1100 / 6100 * 100, places=4)
        self.assertEqual(usd_scope["high_risk_product_pct"], 0.0)
        self.assertAlmostEqual(usd_scope["max_drawdown_pct"], 400 / 6500 * 100, places=4)
        self.assertNotIn(
            "portfolio_risk_budget_thresholds_not_evaluated",
            snapshot["limitations"],
        )

    def test_risk_budget_reports_breaches_without_becoming_unevaluated(self) -> None:
        account_id = self._seed_cached_position()
        self._seed_control_plane()
        self._seed_prior_daily_snapshot(account_id)
        PortfolioRiskPolicyService(repo=self.repo).save_policy(
            {
                "min_cash_buffer_pct": 90,
                "max_single_position_pct": 15,
                "max_sector_pct": 15,
                "max_portfolio_drawdown_pct": 5,
            }
        )

        snapshot = self._service().build(cutoff=datetime(2026, 7, 22, 9, 0, 0))

        self.assertTrue(snapshot["risk_budget"]["evaluated"])
        self.assertEqual(
            {item["code"] for item in snapshot["risk_budget"]["breaches"]},
            {
                "cash_buffer_below_minimum",
                "single_position_above_maximum",
                "sector_exposure_above_maximum",
                "portfolio_drawdown_above_maximum",
            },
        )

    def test_risk_budget_keeps_usd_and_cny_in_independent_scopes(self) -> None:
        usd_account_id = self._seed_cached_position()
        self._seed_control_plane()
        self._seed_prior_daily_snapshot(usd_account_id)
        cny_account = self.repo.create_account(
            name="CNY Account",
            broker="Broker",
            market="cn",
            base_currency="CNY",
        )
        self.repo.replace_positions_lots_and_snapshot(
            account_id=cny_account.id,
            snapshot_date=date(2026, 7, 22),
            cost_method="fifo",
            base_currency="CNY",
            total_cash=900,
            total_market_value=100,
            total_equity=1000,
            unrealized_pnl=0,
            realized_pnl=0,
            fee_total=0,
            tax_total=0,
            fx_stale=False,
            payload="{}",
            positions=[
                {
                    "symbol": "515880",
                    "market": "cn",
                    "currency": "CNY",
                    "quantity": 100,
                    "avg_cost": 1,
                    "total_cost": 100,
                    "last_price": 1,
                    "market_value_base": 100,
                    "unrealized_pnl_base": 0,
                }
            ],
            lots=[],
            valuation_currency="CNY",
        )
        with self.db.get_session() as session:
            session.add(
                PortfolioDailySnapshot(
                    account_id=cny_account.id,
                    snapshot_date=date(2026, 7, 21),
                    cost_method="fifo",
                    base_currency="CNY",
                    total_cash=900,
                    total_market_value=100,
                    total_equity=1000,
                    unrealized_pnl=0,
                    realized_pnl=0,
                    fee_total=0,
                    tax_total=0,
                    fx_stale=False,
                    payload="{}",
                )
            )
            session.commit()
        PortfolioInstrumentService(repo=self.repo).create_instrument(
            {
                "symbol": "515880",
                "market": "cn",
                "quote_currency": "CNY",
                "instrument_type": "etf",
                "trade_lot_size": 100,
                "verification_status": "verified",
                "evidence_source": "https://www.csindex.com.cn/",
                "evidence_as_of": datetime(2026, 7, 22, 7, 0, tzinfo=timezone.utc),
                "metadata": {
                    "risk_sector": {
                        "taxonomy": "portfolio-risk-v1",
                        "as_of": "2026-07-22",
                        "source": "https://www.csindex.com.cn/",
                        "exposures": [
                            {"sector": "Semiconductors", "weight_pct": 100},
                        ],
                    }
                },
            }
        )

        snapshot = self._service().build(cutoff=datetime(2026, 7, 22, 9, 0, 0))

        self.assertTrue(snapshot["risk_budget"]["evaluated"])
        self.assertEqual(
            [scope["currency"] for scope in snapshot["risk_budget"]["scopes"]],
            ["CNY", "USD"],
        )
        cny_scope = snapshot["risk_budget"]["scopes"][0]
        self.assertEqual(cny_scope["total_equity"], 1000.0)
        self.assertEqual(cny_scope["cash_buffer_pct"], 90.0)
        self.assertEqual(cny_scope["max_single_position_pct"], 10.0)

    def test_risk_budget_counts_daily_reset_products_as_high_risk(self) -> None:
        account_id = self._seed_cached_position()
        self._seed_control_plane()
        self._seed_prior_daily_snapshot(account_id, total_equity=1000)
        self.repo.replace_positions_lots_and_snapshot(
            account_id=account_id,
            snapshot_date=date(2026, 7, 22),
            cost_method="fifo",
            base_currency="USD",
            total_cash=900,
            total_market_value=100,
            total_equity=1000,
            unrealized_pnl=0,
            realized_pnl=0,
            fee_total=0,
            tax_total=0,
            fx_stale=False,
            payload="{}",
            positions=[
                {
                    "symbol": "TQQQ",
                    "market": "us",
                    "currency": "USD",
                    "quantity": 1,
                    "avg_cost": 100,
                    "total_cost": 100,
                    "last_price": 100,
                    "market_value_base": 100,
                    "unrealized_pnl_base": 0,
                }
            ],
            lots=[],
            valuation_currency="USD",
        )
        PortfolioInstrumentService(repo=self.repo).create_instrument(
            {
                "symbol": "TQQQ",
                "market": "us",
                "quote_currency": "USD",
                "instrument_type": "daily_leveraged_product",
                "underlying_symbol": "QQQ",
                "underlying_market": "us",
                "underlying_currency": "USD",
                "leverage_factor": 3,
                "daily_reset": True,
                "trade_lot_size": 1,
                "verification_status": "verified",
                "evidence_source": "https://www.proshares.com/our-etfs/leveraged-and-inverse/tqqq",
                "evidence_as_of": datetime(2026, 7, 22, 7, 0, tzinfo=timezone.utc),
                "metadata": {
                    "risk_sector": {
                        "taxonomy": "portfolio-risk-v1",
                        "as_of": "2026-07-22",
                        "source": "https://www.proshares.com/our-etfs/leveraged-and-inverse/tqqq",
                        "exposures": [
                            {"sector": "Technology", "weight_pct": 100},
                        ],
                    }
                },
            }
        )

        snapshot = self._service().build(cutoff=datetime(2026, 7, 22, 9, 0, 0))

        self.assertTrue(snapshot["risk_budget"]["evaluated"])
        usd_scope = snapshot["risk_budget"]["scopes"][0]
        self.assertEqual(usd_scope["high_risk_product_pct"], 10.0)
        self.assertIn(
            "high_risk_product_above_maximum",
            {item["code"] for item in snapshot["risk_budget"]["breaches"]},
        )

    def test_risk_budget_fails_closed_when_sector_evidence_is_missing(self) -> None:
        account_id = self._seed_cached_position()
        self._seed_control_plane()
        self._seed_prior_daily_snapshot(account_id)
        PortfolioInstrumentService(repo=self.repo).update_instrument(
            symbol="AAPL",
            market="us",
            payload={"metadata": {"name": "Apple Inc."}},
        )

        snapshot = self._service().build(cutoff=datetime(2026, 7, 22, 9, 0, 0))

        self.assertFalse(snapshot["risk_budget"]["evaluated"])
        self.assertIn(
            "risk_sector_evidence_missing",
            {item["code"] for item in snapshot["risk_budget"]["evidence_blockers"]},
        )

    def test_risk_budget_fails_closed_when_drawdown_history_is_incomplete(self) -> None:
        self._seed_cached_position()
        self._seed_control_plane()

        snapshot = self._service().build(cutoff=datetime(2026, 7, 22, 9, 0, 0))

        self.assertFalse(snapshot["risk_budget"]["evaluated"])
        self.assertIn(
            "risk_drawdown_history_insufficient",
            {item["code"] for item in snapshot["risk_budget"]["evidence_blockers"]},
        )


if __name__ == "__main__":
    unittest.main()
