# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime

import pytest
from sqlalchemy import select

from src.config import Config
from src.repositories.decision_quality_repo import DecisionQualityRepository
from src.services.decision_execution_link_service import DecisionExecutionLinkService
from src.storage import DatabaseManager, PortfolioAccount, PortfolioTrade


@pytest.fixture()
def isolated_db(tmp_path):
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'execution-links.db'}")
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def _seed_context(db, *, signal_id=101, quantity=100.0, account_id=2):
    return DecisionQualityRepository(db).create_context_if_absent(
        {
            "signal_id": signal_id,
            "account_id": account_id,
            "market": "us",
            "stock_code": "AAPL",
            "instrument_type": "equity",
            "frozen_position_quantity": quantity,
            "frozen_snapshot_hash": f"{signal_id:064x}",
            "material_event_fingerprint": f"{signal_id + 100:064x}",
            "position_action": "reduce",
            "incremental_action": "no_add",
            "confidence_by_horizon_json": json.dumps({"5d": 0.5, "20d": 0.6, "60d": 0.5}),
            "benchmark_market": "us",
            "benchmark_code": "SPY",
            "benchmark_type": "market_index",
            "benchmark_evidence_url": None,
            "benchmark_evidence_as_of": None,
            "decision_cutoff": datetime(2026, 1, 2, 21, 0),
            "decision_market_phase": "postmarket",
            "strategy_version": "champion-v1",
            "context_status": "complete",
            "unable_reasons_json": "[]",
        }
    )[0]


def _seed_trade(
    db,
    *,
    trade_id=None,
    account_id=2,
    trade_date=date(2026, 1, 3),
    side="sell",
    quantity=40.0,
):
    with db.session_scope() as session:
        if session.get(PortfolioAccount, account_id) is None:
            session.add(
                PortfolioAccount(
                    id=account_id,
                    name=f"Account {account_id}",
                    market="us",
                    base_currency="USD",
                )
            )
        trade = PortfolioTrade(
            id=trade_id,
            account_id=account_id,
            symbol="AAPL",
            market="us",
            currency="USD",
            trade_date=trade_date,
            side=side,
            quantity=quantity,
            price=150.0,
            fee=1.0,
            tax=0.2,
            dedup_hash=hashlib.sha256(
                f"{account_id}:{trade_date}:{side}:{quantity}".encode()
            ).hexdigest(),
        )
        session.add(trade)
        session.flush()
        return int(trade.id)


def _ledger_snapshot(db):
    with db.get_session() as session:
        rows = session.execute(select(PortfolioTrade).order_by(PortfolioTrade.id)).scalars().all()
        payload = [
            {
                "id": row.id,
                "account_id": row.account_id,
                "symbol": row.symbol,
                "market": row.market,
                "trade_date": row.trade_date.isoformat(),
                "side": row.side,
                "quantity": row.quantity,
                "price": row.price,
                "fee": row.fee,
                "tax": row.tax,
            }
            for row in rows
        ]
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return len(payload), digest


def test_confirmed_partial_sell_derives_reduce_without_mutating_ledger(isolated_db) -> None:
    _seed_context(isolated_db)
    trade_id = _seed_trade(isolated_db, quantity=40.0)
    before = _ledger_snapshot(isolated_db)

    result = DecisionExecutionLinkService(db_manager=isolated_db).put_link(
        signal_id=101,
        trade_id=trade_id,
        link_status="confirmed",
        linked_by="human",
        note="Trade occurred after the frozen recommendation.",
    )

    assert result["link"]["temporal_relation"] == "after_signal_confirmed"
    assert result["actual_position_action"] == "reduce"
    assert result["actual_incremental_action"] is None
    assert _ledger_snapshot(isolated_db) == before


def test_same_day_trade_requires_human_ordering_confirmation(isolated_db) -> None:
    _seed_context(isolated_db)
    trade_id = _seed_trade(isolated_db, trade_date=date(2026, 1, 2))
    service = DecisionExecutionLinkService(db_manager=isolated_db)

    unknown = service.put_link(
        signal_id=101,
        trade_id=trade_id,
        link_status="proposed",
        linked_by="import",
    )

    assert unknown["link"]["temporal_relation"] == "same_day_unknown"
    assert unknown["actual_position_action"] is None


def test_one_trade_cannot_be_confirmed_for_two_strategy_events(isolated_db) -> None:
    _seed_context(isolated_db, signal_id=101)
    _seed_context(isolated_db, signal_id=102)
    trade_id = _seed_trade(isolated_db)
    service = DecisionExecutionLinkService(db_manager=isolated_db)
    service.put_link(
        signal_id=101,
        trade_id=trade_id,
        link_status="confirmed",
        linked_by="human",
    )

    with pytest.raises(ValueError, match="trade_already_attributed"):
        service.put_link(
            signal_id=102,
            trade_id=trade_id,
            link_status="confirmed",
            linked_by="human",
        )


def test_trade_must_match_frozen_account_and_instrument(isolated_db) -> None:
    _seed_context(isolated_db, account_id=2)
    trade_id = _seed_trade(isolated_db, account_id=3)

    with pytest.raises(ValueError, match="trade_account_mismatch"):
        DecisionExecutionLinkService(db_manager=isolated_db).put_link(
            signal_id=101,
            trade_id=trade_id,
            link_status="confirmed",
            linked_by="human",
        )


def test_confirmed_buy_derives_incremental_action_only(isolated_db) -> None:
    _seed_context(isolated_db)
    trade_id = _seed_trade(isolated_db, side="buy", quantity=20.0)

    result = DecisionExecutionLinkService(db_manager=isolated_db).put_link(
        signal_id=101,
        trade_id=trade_id,
        link_status="confirmed",
        linked_by="human",
    )

    assert result["actual_position_action"] is None
    assert result["actual_incremental_action"] == "add_in_batches"
