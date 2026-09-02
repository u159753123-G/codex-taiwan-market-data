#!/usr/bin/env python3
"""抓取 TWSE／TPEx 官方盤後資料，輸出 Portfolio 專用行情 JSON。"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_URL = "https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&se=EW&o=data"
TAIEX_HISTORY_URL = "https://www.twse.com.tw/indicesReport/MI_5MINS_HIST?response=json&date={year:04d}{month:02d}01"
HEADERS = {"User-Agent": "codex-taiwan-portfolio/1.0"}
MIN_RECORDS = {"TWSE": 500, "TPEx": 300}
MARKET_RETRY_DELAYS = (2, 4, 8)


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def decode_bytes(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp950", "big5"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ValueError("資料不是支援的 UTF-8／CP950／Big5 編碼")


def to_number(value: Any) -> int | float | None:
    if value is None:
        return None
    normalized = str(value).replace(",", "").replace("%", "").strip()
    if normalized in {"", "-", "--", "X", "N/A", "除權", "除息"}:
        return None
    try:
        number = float(normalized)
        return int(number) if number.is_integer() else number
    except ValueError:
        return None


def roc_to_iso(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 5:
        return None
    year_digits, mmdd = digits[:-4], digits[-4:]
    try:
        year = int(year_digits) + 1911
        month, day = int(mmdd[:2]), int(mmdd[2:])
        datetime(year, month, day)
        return f"{year:04d}-{month:02d}-{day:02d}"
    except ValueError:
        return None


def find_column(fieldnames: list[str], keywords: tuple[str, ...]) -> str | None:
    return next((name for name in fieldnames if any(keyword in name for keyword in keywords)), None)


def normalize_row(
    *, symbol: Any, name: Any, market: str, close: Any, open_price: Any,
    high: Any, low: Any, volume: Any, trade_value: Any, trade_date: str | None,
) -> dict[str, Any] | None:
    normalized_symbol = str(symbol or "").strip().strip('="')
    if not normalized_symbol:
        return None
    return {
        "symbol": normalized_symbol,
        "name": str(name or "").strip(),
        "market": market,
        "close": to_number(close),
        "open": to_number(open_price),
        "high": to_number(high),
        "low": to_number(low),
        "volume": to_number(volume),
        "value": to_number(trade_value),
        "tradeDate": trade_date,
        "source": market,
        "status": "fresh",
    }


def parse_twse(raw: bytes) -> tuple[dict[str, dict[str, Any]], str | None]:
    rows = json.loads(decode_bytes(raw))
    if not isinstance(rows, list):
        raise ValueError("TWSE 回傳格式不是 JSON array")
    symbols: dict[str, dict[str, Any]] = {}
    dates: set[str] = set()
    for source in rows:
        trade_date = roc_to_iso(source.get("Date"))
        if trade_date:
            dates.add(trade_date)
        row = normalize_row(
            symbol=source.get("Code"), name=source.get("Name"), market="TWSE",
            close=source.get("ClosingPrice"), open_price=source.get("OpeningPrice"),
            high=source.get("HighestPrice"), low=source.get("LowestPrice"),
            volume=source.get("TradeVolume"), trade_value=source.get("TradeValue"),
            trade_date=trade_date,
        )
        if row:
            symbols[row["symbol"]] = row
    if len(dates) > 1:
        raise ValueError(f"TWSE 同批資料含多個交易日: {sorted(dates)}")
    return symbols, next(iter(dates), None)


def parse_tpex(raw: bytes) -> tuple[dict[str, dict[str, Any]], str | None]:
    lines = [line for line in decode_bytes(raw).splitlines() if line.strip()]
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    rows = list(reader)
    fields = [str(name).strip() for name in (reader.fieldnames or [])]
    columns = {
        "symbol": find_column(fields, ("證券代號", "股票代號", "代號")),
        "name": find_column(fields, ("證券名稱", "名稱")),
        "close": find_column(fields, ("收盤價", "收盤")),
        "open": find_column(fields, ("開盤價", "開盤")),
        "high": find_column(fields, ("最高價", "最高")),
        "low": find_column(fields, ("最低價", "最低")),
        "volume": find_column(fields, ("成交股數", "成交量")),
        "value": find_column(fields, ("成交金額",)),
        "date": find_column(fields, ("資料日期", "日期")),
    }
    if not columns["symbol"] or not columns["close"]:
        raise ValueError(f"TPEx 關鍵欄位比對失敗: {fields}")
    symbols: dict[str, dict[str, Any]] = {}
    dates: set[str] = set()
    for source in rows:
        trade_date = roc_to_iso(source.get(columns["date"])) if columns["date"] else None
        if trade_date:
            dates.add(trade_date)
        row = normalize_row(
            symbol=source.get(columns["symbol"]),
            name=source.get(columns["name"]) if columns["name"] else "",
            market="TPEx", close=source.get(columns["close"]),
            open_price=source.get(columns["open"]) if columns["open"] else None,
            high=source.get(columns["high"]) if columns["high"] else None,
            low=source.get(columns["low"]) if columns["low"] else None,
            volume=source.get(columns["volume"]) if columns["volume"] else None,
            trade_value=source.get(columns["value"]) if columns["value"] else None,
            trade_date=trade_date,
        )
        if row:
            symbols[row["symbol"]] = row
    if len(dates) > 1:
        raise ValueError(f"TPEx 同批資料含多個交易日: {sorted(dates)}")
    return symbols, next(iter(dates), None)


def parse_taiex_history(raw: bytes) -> list[dict[str, Any]]:
    payload = json.loads(decode_bytes(raw))
    if payload.get("stat") != "OK" or not isinstance(payload.get("data"), list):
        raise ValueError(f"TAIEX 歷史資料格式錯誤: {payload.get('stat', 'unknown')}")
    prices: list[dict[str, Any]] = []
    for source in payload["data"]:
        if not isinstance(source, list) or len(source) < 5:
            continue
        traded_on = roc_to_iso(source[0])
        close = to_number(source[4])
        if not traded_on or close is None:
            continue
        prices.append({
            "date": traded_on,
            "open": to_number(source[1]),
            "high": to_number(source[2]),
            "low": to_number(source[3]),
            "close": close,
        })
    if not prices:
        raise ValueError("TAIEX 歷史資料沒有有效收盤指數")
    return prices


def update_taiex_history(output_dir: Path) -> dict[str, Any]:
    history_path = output_dir / "codex_taiex_history.json"
    previous = load_json(history_path) or {}
    merged = {row["date"]: row for row in previous.get("prices", []) if row.get("date")}
    now = datetime.now(timezone.utc)
    months = [(now.year, month) for month in range(1, now.month + 1)] if not merged else [(now.year, now.month)]
    errors: list[str] = []
    fetched = 0
    for year, month in months:
        try:
            rows = parse_taiex_history(fetch_bytes(TAIEX_HISTORY_URL.format(year=year, month=month)))
            merged.update((row["date"], row) for row in rows)
            fetched += len(rows)
        except Exception as error:
            errors.append(f"{year:04d}-{month:02d}: {error}")
    prices = [merged[date] for date in sorted(merged)]
    result = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ticker": "IX0001",
        "name": "發行量加權股價指數",
        "source": "TWSE",
        "status": "fresh" if fetched else ("stale" if prices else "failed"),
        "latest": prices[-1] if prices else None,
        "prices": prices,
        "errors": errors,
    }
    atomic_json_write(history_path, result)
    return result


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def atomic_json_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def publish_json(url: str, token: str, data: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            **HEADERS,
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(decode_bytes(response.read()))


def validate_market(market: str, symbols: dict[str, dict[str, Any]]) -> None:
    if len(symbols) < MIN_RECORDS[market]:
        raise ValueError(f"筆數異常過少（{len(symbols)} 筆）")
    if not any(row.get("close") is not None for row in symbols.values()):
        raise ValueError("所有收盤價皆為空值")


def fetch_market(
    url: str,
    parser: Any,
    retry_delays: tuple[int, ...] = (),
) -> tuple[dict[str, dict[str, Any]], str | None, int]:
    attempt = 0
    while True:
        attempt += 1
        try:
            symbols, trade_date = parser(fetch_bytes(url))
            return symbols, trade_date, attempt
        except Exception as error:
            if attempt > len(retry_delays):
                raise RuntimeError(f"連續 {attempt} 次請求失敗：{error}") from error
            time.sleep(retry_delays[attempt - 1])


def previous_market(previous: dict[str, Any], market: str) -> dict[str, dict[str, Any]]:
    retained: dict[str, dict[str, Any]] = {}
    for symbol, source in previous.get("symbols", {}).items():
        if source.get("market") == market:
            retained[symbol] = dict(source)
    return retained


def run(output_dir: Path, keep_snapshots: bool = True) -> dict[str, Any]:
    latest_path = output_dir / "codex_stock_prices_latest.json"
    previous = load_json(latest_path) or {}
    combined: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    market_status: dict[str, dict[str, Any]] = {}
    trade_dates: list[str] = []
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for market, url, parser in (
        ("TWSE", TWSE_URL, parse_twse),
        ("TPEx", TPEX_URL, parse_tpex),
    ):
        try:
            symbols, trade_date, attempts = fetch_market(url, parser, MARKET_RETRY_DELAYS)
            validate_market(market, symbols)
            combined.update(symbols)
            if trade_date:
                trade_dates.append(trade_date)
            market_status[market] = {
                "status": "success",
                "fetchStatus": "success",
                "dataStatus": "fresh",
                "tradeDate": trade_date,
                "recordCount": len(symbols),
                "attemptCount": attempts,
                "consecutiveFetchFailures": 0,
                "lastAttemptAt": generated_at,
                "lastSuccessAt": generated_at,
                "error": None,
            }
        except Exception as error:
            fallback = previous_market(previous, market)
            combined.update(fallback)
            errors.append({"market": market, "message": str(error)})
            previous_status = previous.get("markets", {}).get(market, {})
            retained_data_status = previous_status.get("dataStatus")
            if retained_data_status not in {"fresh", "stale", "missing"}:
                retained_data_status = "fresh" if any(row.get("status") == "fresh" for row in fallback.values()) else ("stale" if fallback else "missing")
            market_status[market] = {
                "status": "stale" if fallback else "failed",
                "fetchStatus": "failed",
                "dataStatus": retained_data_status,
                "tradeDate": next((row.get("tradeDate") for row in fallback.values()), None),
                "recordCount": len(fallback),
                "attemptCount": len(MARKET_RETRY_DELAYS) + 1,
                "consecutiveFetchFailures": int(previous_status.get("consecutiveFetchFailures") or 0) + 1,
                "lastAttemptAt": generated_at,
                "lastSuccessAt": previous_status.get("lastSuccessAt") or (previous.get("generatedAt") if fallback else None),
                "error": str(error),
            }

    if not combined:
        raise RuntimeError("兩個市場皆抓取失敗，且沒有舊行情可沿用")

    trade_date = max(trade_dates) if trade_dates else previous.get("tradeDate")
    result = {
        "generatedAt": generated_at,
        "tradeDate": trade_date,
        "markets": market_status,
        "symbols": combined,
        "errors": errors,
    }
    atomic_json_write(latest_path, result)
    if keep_snapshots and trade_date:
        atomic_json_write(output_dir / f"codex_stock_prices_{trade_date}.json", result)
    snapshot_dates = sorted(
        (match.group(1) for path in output_dir.glob("codex_stock_prices_????-??-??.json")
         if (match := re.search(r"codex_stock_prices_(\d{4}-\d{2}-\d{2})\.json$", path.name))),
        reverse=True,
    )
    atomic_json_write(output_dir / "codex_trade_dates.json", {"dates": snapshot_dates})
    benchmark = update_taiex_history(output_dir)
    result["benchmark"] = {
        "ticker": benchmark["ticker"],
        "status": benchmark["status"],
        "latest": benchmark["latest"],
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("data/market"))
    parser.add_argument(
        "--latest-only",
        action="store_true",
        help="只更新 latest JSON，不保留每日全市場快照",
    )
    parser.add_argument("--publish-url", help="完成後 POST 至網站行情匯入 API")
    parser.add_argument("--token-env", default="CODEX_MARKET_INGEST_TOKEN", help="匯入 token 的環境變數名稱")
    parser.add_argument("--fail-on-fetch-error", action="store_true", help="所有重試耗盡後以非零狀態結束")
    args = parser.parse_args()
    result = run(args.output_dir, keep_snapshots=not args.latest_only)
    fresh = sum(row["status"] == "fresh" for row in result["symbols"].values())
    stale = sum(row["status"] == "stale" for row in result["symbols"].values())
    print(f"完成：{len(result['symbols'])} 檔（fresh {fresh}／stale {stale}），交易日 {result['tradeDate']}")
    benchmark = result.get("benchmark", {})
    print(f"大盤：{benchmark.get('ticker')} {benchmark.get('status')}，最新 {benchmark.get('latest')}")
    for error in result["errors"]:
        print(f"[{error['market']}] {error['message']}")
    if args.publish_url:
        token = os.environ.get(args.token_env)
        if not token:
            raise RuntimeError(f"缺少環境變數 {args.token_env}")
        response = publish_json(args.publish_url, token, result)
        print(f"網站匯入完成：{response.get('imported', 0)} 檔")
    if args.fail_on_fetch_error and any(item.get("fetchStatus") == "failed" for item in result["markets"].values()):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
