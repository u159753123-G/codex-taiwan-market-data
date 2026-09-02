import importlib.util
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / "scripts" / "market_data" / "codex_fetch_stock_prices.py"
SPEC = importlib.util.spec_from_file_location("codex_prices", SCRIPT)
prices = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(prices)


class PriceNormalizationTests(unittest.TestCase):
    def test_symbol_preserves_leading_zero_and_suffix(self):
        row = prices.normalize_row(
            symbol="0050", name="元大台灣50", market="TWSE", close="100.5",
            open_price="100", high="101", low="99", volume="1,000",
            trade_value="100,500", trade_date="2026-08-13",
        )
        self.assertEqual(row["symbol"], "0050")
        self.assertEqual(row["close"], 100.5)
        self.assertEqual(prices.normalize_row(
            symbol="00631L", name="正2", market="TWSE", close="--", open_price=None,
            high=None, low=None, volume=None, trade_value=None, trade_date=None,
        )["symbol"], "00631L")

    def test_missing_price_is_null_not_zero(self):
        self.assertIsNone(prices.to_number("--"))
        self.assertIsNone(prices.to_number(""))
        self.assertEqual(prices.to_number("0"), 0)

    def test_roc_date_conversion(self):
        self.assertEqual(prices.roc_to_iso("115/08/13"), "2026-08-13")
        self.assertEqual(prices.roc_to_iso("1150813"), "2026-08-13")
        self.assertIsNone(prices.roc_to_iso("invalid"))

    def test_twse_parser(self):
        payload = json.dumps([{
            "Date": "1150813", "Code": "0050", "Name": "元大台灣50",
            "TradeVolume": "1,000", "TradeValue": "100,000",
            "OpeningPrice": "99", "HighestPrice": "101", "LowestPrice": "98",
            "ClosingPrice": "100", "Change": "+1", "Transaction": "10",
        }], ensure_ascii=False).encode()
        symbols, trade_date = prices.parse_twse(payload)
        self.assertEqual(trade_date, "2026-08-13")
        self.assertEqual(symbols["0050"]["market"], "TWSE")

    def test_tpex_parser(self):
        payload = "資料日期,證券代號,證券名稱,收盤價,開盤價,最高價,最低價,成交股數,成交金額\n115/08/13,7828,創新服務,1,910,1900,1920,1880,1,000,1910000\n"
        # CSV 中含千分位時官方資料會以引號包覆；fixture 避免製造無效 CSV。
        payload = "資料日期,證券代號,證券名稱,收盤價,開盤價,最高價,最低價,成交股數,成交金額\n115/08/13,7828,創新服務,1910,1900,1920,1880,1000,1910000\n"
        symbols, trade_date = prices.parse_tpex(payload.encode())
        self.assertEqual(trade_date, "2026-08-13")
        self.assertEqual(symbols["7828"]["close"], 1910)

    def test_taiex_history_parser(self):
        payload = json.dumps({
            "stat": "OK",
            "data": [["115/08/12", "45,175.70", "45,529.48", "45,175.70", "45,518.07"]],
        }, ensure_ascii=False).encode()
        rows = prices.parse_taiex_history(payload)
        self.assertEqual(rows, [{
            "date": "2026-08-12", "open": 45175.7, "high": 45529.48,
            "low": 45175.7, "close": 45518.07,
        }])

    def test_taiex_history_rejects_empty_payload(self):
        with self.assertRaisesRegex(ValueError, "沒有有效收盤指數"):
            prices.parse_taiex_history(json.dumps({"stat": "OK", "data": []}).encode())


class FallbackTests(unittest.TestCase):
    def test_failed_market_keeps_previous_price_status(self):
        previous = {"symbols": {"2330": {"symbol": "2330", "market": "TWSE", "close": 1000, "status": "fresh"}}}
        retained = prices.previous_market(previous, "TWSE")
        self.assertEqual(retained["2330"]["status"], "fresh")
        self.assertEqual(retained["2330"]["close"], 1000)

    def test_twse_retries_three_times_after_initial_failure(self):
        payload = json.dumps([{
            "Date": "1150901", "Code": "2330", "Name": "台積電",
            "TradeVolume": "1", "TradeValue": "1", "OpeningPrice": "1",
            "HighestPrice": "1", "LowestPrice": "1", "ClosingPrice": "1",
        }]).encode()
        with patch.object(prices, "fetch_bytes", side_effect=[ValueError("1"), ValueError("2"), ValueError("3"), payload]) as fetch, \
             patch.object(prices.time, "sleep") as sleep:
            symbols, trade_date, attempts = prices.fetch_market("https://example.test", prices.parse_twse, (2, 4, 8))
        self.assertEqual(attempts, 4)
        self.assertEqual(trade_date, "2026-09-01")
        self.assertEqual(symbols["2330"]["status"], "fresh")
        self.assertEqual(fetch.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4, 8])

    def test_fetch_failure_is_separate_from_retained_data_status(self):
        previous = {
            "generatedAt": "2026-09-01T12:00:00+00:00",
            "markets": {"TWSE": {"dataStatus": "fresh", "lastSuccessAt": "2026-09-01T12:00:00+00:00"}},
            "symbols": {"2330": {"symbol": "2330", "market": "TWSE", "close": 1000, "status": "fresh", "tradeDate": "2026-09-01"}},
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            prices.atomic_json_write(output / "codex_stock_prices_latest.json", previous)
            tpex = {str(index): {"symbol": str(index), "market": "TPEx", "close": 1, "status": "fresh", "tradeDate": "2026-09-02"} for index in range(300)}
            with patch.object(prices, "fetch_market", side_effect=[RuntimeError("TWSE unavailable"), (tpex, "2026-09-02", 1)]), \
                 patch.object(prices, "update_taiex_history", return_value={"ticker": "IX0001", "status": "fresh", "latest": None}):
                result = prices.run(output, keep_snapshots=False)
        self.assertEqual(result["markets"]["TWSE"]["fetchStatus"], "failed")
        self.assertEqual(result["markets"]["TWSE"]["dataStatus"], "fresh")
        self.assertEqual(result["symbols"]["2330"]["status"], "fresh")
        self.assertEqual(result["markets"]["TWSE"]["consecutiveFetchFailures"], 1)

    def test_atomic_write_produces_valid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "prices.json"
            prices.atomic_json_write(target, {"symbols": {"0050": {"close": None}}})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["symbols"]["0050"]["close"], None)

    def test_publish_uses_bearer_token_and_json(self):
        captured = {}

        @contextmanager
        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            class Response:
                def read(self):
                    return b'{"ok":true,"imported":1}'
            yield Response()

        with patch.object(prices.urllib.request, "urlopen", fake_urlopen):
            response = prices.publish_json("https://example.test/api/market-prices", "secret", {"symbols": {"0050": {}}})
        self.assertEqual(response["imported"], 1)
        self.assertEqual(captured["request"].get_header("Authorization"), "Bearer secret")
        self.assertEqual(captured["request"].method, "POST")


if __name__ == "__main__":
    unittest.main()
