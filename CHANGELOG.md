# Changelog

本專案的重要變更會記錄在此文件。格式參考 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.1.0/)，版本號採用 [Semantic Versioning](https://semver.org/lang/zh-TW/)。

## [0.3.0] - 2026-11-07

### 新增

- 設定頁可下載備份檔，也可上傳備份檔再還原；換電腦時用一份檔案就能搬移全部資料，還原後顯示交易、帳戶等筆數摘要；設定頁顯示資料夾位置
- 同步中的按鈕會變成「取消同步」，確認後停止；同步任務頁也可取消。已完成的項目會保留，可再重試
- `taiwan-stock web --trusted-host <名稱>`：搭配 Tailscale 等只有自己裝置連得到的轉送，從手機或其他電腦連線

### 變更

- 取消同步（含 CLI 的 Ctrl+C）會在請求之間就停止，不必等整個步驟完成
- 更新程式後，瀏覽器會自動載入新的 JS／CSS，不再沿用快取的舊檔
- 開發：測試可平行執行（`pytest -n auto --dist=loadfile`）

## [0.2.0] - 2026-11-06

### 新增

- 交易表單的股數、價格、金額與費用欄位，以及試算預覽、同步筆數等數字以千分位顯示
- clone 後從原始碼執行（`pip install -e .`）時，資料改存在專案內的 `dev-data/`，開發時不會讀寫安裝版的資料；以 `pip install .`、`pipx` 或 `uv tool` 安裝時位置不變

### 修正

- ETF 配息來源把 1 月除息歸到前一年時，1 月配息不再缺漏或被誤標待確認

### 升級注意

- 若 0.1.0 是以 `pip install -e .` 安裝並當作日常使用，升級後會看到空的資料庫；原資料仍在 `%LOCALAPPDATA%\TaiwanStock\data`（Windows）或 `~/.local/share/taiwan-stock`（macOS / Linux）。可用 `--data-dir` 或環境變數 `TSL_DATA_DIR` 指向原資料夾，或改以 `pip install .` 安裝

## [0.1.0] - 2026-11-02

### 新增

- 本機 Web 介面（`taiwan-stock web`），使用 SQLite 儲存資料
- 我的持股：市值、損益、報酬率、成本殖利率，以及含費用回本價、扣息回本價與賣出試算
- 商品查詢：同步 TWSE／TPEx 股票與 ETF 名單、上下市狀態及 ETF 官方基本資料
- 交易紀錄：依商品與日期自動試算費用，支援篩選、CSV 匯出與預覽後匯入
- 股票異動：自動同步分割與股票股利
- 領息月曆：依持股試算配息，可依證券帳戶或領息銀行篩選
- 背景同步任務與 `taiwan-stock sync` CLI 指令
- 本機資料庫備份與還原
- 初版公開發布
