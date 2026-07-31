# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import importlib
import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import Config
from src.repositories.portfolio_repo import PortfolioRepository
from src.storage import DatabaseManager


class PortfolioInstrumentServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "instrument_service.db"
        os.environ["DATABASE_PATH"] = str(self.db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        self.temp_dir.cleanup()

    def _service(self):
        module_name = "src.services.portfolio_instrument_service"
        self.assertIsNotNone(
            importlib.util.find_spec(module_name),
            "portfolio instrument service module is required",
        )
        module = importlib.import_module(module_name)
        return module.PortfolioInstrumentService(
            repo=PortfolioRepository(db_manager=self.db)
        )

    def test_create_normalizes_canonical_identity_without_auto_verification(self) -> None:
        created = self._service().create_instrument(
            {
                "symbol": " aapl ",
                "market": " US ",
                "quote_currency": " usd ",
                "instrument_type": " EQUITY ",
                "trade_lot_size": 1,
                "metadata": {"issuer": "Apple"},
            }
        )

        self.assertEqual(created["symbol"], "AAPL")
        self.assertEqual(created["market"], "us")
        self.assertEqual(created["quote_currency"], "USD")
        self.assertEqual(created["instrument_type"], "equity")
        self.assertEqual(created["verification_status"], "missing")
        self.assertEqual(created["metadata"], {"issuer": "Apple"})

    def test_verified_identity_requires_dated_evidence(self) -> None:
        base = {
            "symbol": "AAPL",
            "market": "us",
            "quote_currency": "USD",
            "instrument_type": "equity",
            "trade_lot_size": 1,
            "verification_status": "verified",
        }

        with self.assertRaisesRegex(ValueError, "evidence_source"):
            self._service().create_instrument(base)

        with self.assertRaisesRegex(ValueError, "evidence_as_of"):
            self._service().create_instrument({**base, "evidence_source": "NASDAQ"})

    def test_evidence_as_of_requires_timezone_and_serializes_explicit_utc(self) -> None:
        base = {
            "symbol": "AAPL",
            "market": "us",
            "quote_currency": "USD",
            "instrument_type": "equity",
            "trade_lot_size": 1,
            "verification_status": "verified",
            "evidence_source": "NASDAQ symbol directory",
        }

        with self.assertRaisesRegex(ValueError, "timezone"):
            self._service().create_instrument(
                {**base, "evidence_as_of": datetime(2026, 7, 22, 9, 0, 0)}
            )

        created = self._service().create_instrument(
            {
                **base,
                "evidence_as_of": datetime(
                    2026,
                    7,
                    22,
                    9,
                    0,
                    0,
                    tzinfo=timezone(timedelta(hours=8)),
                ),
            }
        )

        self.assertEqual(created["evidence_as_of"], "2026-07-22T01:00:00+00:00")

    def test_rejects_invalid_instrument_and_verification_enums(self) -> None:
        base = {
            "symbol": "AAPL",
            "market": "us",
            "quote_currency": "USD",
            "instrument_type": "option",
            "trade_lot_size": 1,
        }

        with self.assertRaisesRegex(ValueError, "instrument_type"):
            self._service().create_instrument(base)

        with self.assertRaisesRegex(ValueError, "verification_status"):
            self._service().create_instrument(
                {
                    **base,
                    "instrument_type": "equity",
                    "verification_status": "trusted",
                }
            )

    def test_daily_leveraged_product_requires_verified_structure(self) -> None:
        base = {
            "symbol": "HK07709",
            "market": "hk",
            "quote_currency": "HKD",
            "instrument_type": "daily_leveraged_product",
            "trade_lot_size": 100,
            "verification_status": "provisional",
        }

        with self.assertRaisesRegex(ValueError, "underlying_symbol"):
            self._service().create_instrument(base)

        with self.assertRaisesRegex(ValueError, "daily_reset"):
            self._service().create_instrument(
                {
                    **base,
                    "underlying_symbol": "000660.KS",
                    "underlying_market": "kr",
                    "underlying_currency": "KRW",
                    "leverage_factor": 2,
                }
            )

    def test_qdii_and_adr_require_product_specific_parity_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires_premium_check"):
            self._service().create_instrument(
                {
                    "symbol": "513100",
                    "market": "cn",
                    "quote_currency": "CNY",
                    "instrument_type": "qdii",
                    "trade_lot_size": 100,
                }
            )

        with self.assertRaisesRegex(ValueError, "conversion_ratio"):
            self._service().create_instrument(
                {
                    "symbol": "TSM",
                    "market": "us",
                    "quote_currency": "USD",
                    "instrument_type": "adr_ads",
                    "underlying_symbol": "2330.TW",
                    "underlying_market": "tw",
                    "underlying_currency": "TWD",
                    "trade_lot_size": 1,
                    "requires_premium_check": True,
                }
            )

    def test_partial_update_preserves_identity_and_revalidates_record(self) -> None:
        service = self._service()
        created = service.create_instrument(
            {
                "symbol": "AAPL",
                "market": "us",
                "quote_currency": "USD",
                "instrument_type": "equity",
                "trade_lot_size": 1,
            }
        )

        updated = service.update_instrument(
            symbol="aapl",
            market="US",
            payload={
                "verification_status": "verified",
                "evidence_source": "NASDAQ symbol directory",
                "evidence_as_of": datetime(2026, 7, 22, 8, 0, 0, tzinfo=timezone.utc),
            },
        )

        self.assertEqual(updated["id"], created["id"])
        self.assertEqual(updated["symbol"], "AAPL")
        self.assertEqual(updated["verification_status"], "verified")
        self.assertEqual(updated["trade_lot_size"], 1.0)

    def test_partial_update_of_verified_identity_reuses_stored_utc_evidence_time(self) -> None:
        service = self._service()
        service.create_instrument(
            {
                "symbol": "AAPL",
                "market": "us",
                "quote_currency": "USD",
                "instrument_type": "equity",
                "trade_lot_size": 1,
                "verification_status": "verified",
                "evidence_source": "NASDAQ symbol directory",
                "evidence_as_of": datetime(2026, 7, 22, 8, 0, 0, tzinfo=timezone.utc),
            }
        )

        updated = service.update_instrument(
            symbol="AAPL",
            market="us",
            payload={"metadata": {"name": "Apple Inc."}},
        )

        self.assertEqual(updated["evidence_as_of"], "2026-07-22T08:00:00+00:00")
        self.assertEqual(updated["metadata"], {"name": "Apple Inc."})

    def test_risk_sector_metadata_requires_weighted_dated_evidence(self) -> None:
        base = {
            "symbol": "AAPL",
            "market": "us",
            "quote_currency": "USD",
            "instrument_type": "equity",
            "trade_lot_size": 1,
        }
        service = self._service()

        with self.assertRaisesRegex(ValueError, "risk_sector.taxonomy"):
            service.create_instrument(
                {
                    **base,
                    "metadata": {
                        "risk_sector": {
                            "taxonomy": "gics",
                            "as_of": "2026-07-23",
                            "source": "https://investor.example/aapl",
                            "exposures": [
                                {"sector": "Technology Hardware", "weight_pct": 100},
                            ],
                        }
                    },
                }
            )

        with self.assertRaisesRegex(ValueError, "risk_sector.as_of"):
            service.create_instrument(
                {
                    **base,
                    "metadata": {
                        "risk_sector": {
                            "taxonomy": "portfolio-risk-v1",
                            "as_of": "July 23",
                            "source": "https://investor.example/aapl",
                            "exposures": [
                                {"sector": "Technology Hardware", "weight_pct": 100},
                            ],
                        }
                    },
                }
            )

        with self.assertRaisesRegex(ValueError, "risk_sector.source"):
            service.create_instrument(
                {
                    **base,
                    "metadata": {
                        "risk_sector": {
                            "taxonomy": "portfolio-risk-v1",
                            "as_of": "2026-07-23",
                            "exposures": [
                                {"sector": "Technology Hardware", "weight_pct": 100},
                            ],
                        }
                    },
                }
            )

        with self.assertRaisesRegex(ValueError, "risk_sector.exposures"):
            service.create_instrument(
                {
                    **base,
                    "metadata": {
                        "risk_sector": {
                            "taxonomy": "portfolio-risk-v1",
                            "as_of": "2026-07-23",
                            "source": "https://investor.example/aapl",
                            "exposures": [],
                        }
                    },
                }
            )

        with self.assertRaisesRegex(ValueError, "total 100"):
            service.create_instrument(
                {
                    **base,
                    "metadata": {
                        "risk_sector": {
                            "taxonomy": "portfolio-risk-v1",
                            "as_of": "2026-07-23",
                            "source": "https://investor.example/aapl",
                            "exposures": [
                                {"sector": "Technology Hardware", "weight_pct": 90},
                            ],
                        }
                    },
                }
            )

        created = service.create_instrument(
            {
                **base,
                "metadata": {
                    "name": "Apple Inc.",
                    "risk_sector": {
                        "taxonomy": "portfolio-risk-v1",
                        "as_of": "2026-07-23",
                        "source": "https://investor.example/aapl",
                        "exposures": [
                            {"sector": "Technology Hardware", "weight_pct": 100},
                        ],
                    },
                },
            }
        )

        self.assertEqual(
            created["metadata"]["risk_sector"],
            {
                "taxonomy": "portfolio-risk-v1",
                "as_of": "2026-07-23",
                "source": "https://investor.example/aapl",
                "exposures": [
                    {"sector": "Technology Hardware", "weight_pct": 100.0},
                ],
            },
        )


if __name__ == "__main__":
    unittest.main()
