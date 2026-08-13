# CODEX 台股盤後行情

這個公開 Repository 為台股 Portfolio App 提供統一格式的盤後行情 JSON。資料只來自官方開放資料，不使用 Yahoo 爬蟲，也不包含任何持股、本金或帳戶資料。

## 資料來源

- 上市：TWSE `STOCK_DAY_ALL` OpenAPI
- 上櫃：TPEx 盤後行情 CSV

## 公開資料

- `data/market/codex_stock_prices_latest.json`：最新上市、上櫃行情
- `data/market/codex_trade_dates.json`：本 Repo 保留的快照日期；自動排程採 latest-only，因此不累積每日全市場快照

每筆股票統一包含 `symbol`、`name`、`market`、`close`、`open`、`high`、`low`、`volume`、`value`、`tradeDate`、`source` 與 `status`。

## 自動更新

GitHub Actions 在台北時間每個交易日 15:35 與 19:25 執行，也可從 Actions 頁面手動執行。流程會先跑單元測試，再抓取 TWSE／TPEx；若單一市場暫時失敗，會沿用上一份該市場資料並標示為 `stale`。

本專案僅提供資料整理與追蹤用途，不構成投資建議。
