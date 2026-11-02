# 台灣股市數據工具（Taiwan Stock）

Taiwan Stock 是一個本機執行的 Python Web 應用（FastAPI），用來查詢台灣上市（TWSE）與上櫃（TPEx）股票及 ETF、記錄個人交易與持股，並自動同步除權息公告與 ETF 配息，試算含費用回本價與領息月曆。本專案並非證交所或櫃買中心官方服務，資料以官方公告為準，試算結果不構成投資建議。

## 專案概覽

| 項目 | 內容 |
| :--- | :--- |
| **核心目標** | 查詢上市櫃股票及 ETF，記錄交易與持股，試算回本價、殖利率與領息月曆 |
| **專案類型** | Python Web 應用（FastAPI + SQLite），本機單人使用 |
| **資料來源** | 交易紀錄手動輸入；商品名單、股價、配息、股票股利、上下市狀態等盡量自動同步 TWSE／TPEx／MOPS 官方資料 |
| **部署目標** | 以 Windows 本機為主，也支援 macOS / Linux；Web 介面僅供本機存取 |

## 快速開始

### 1. 安裝

需要 Python 3.13。

Windows PowerShell：

```powershell
git clone git@github.com:tuchiayu/taiwan-stock-pub.git
Set-Location taiwan-stock-pub
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-dev.txt
pip install -e .
Copy-Item .env.example .env
```

macOS / Linux：

```bash
git clone git@github.com:tuchiayu/taiwan-stock-pub.git
cd taiwan-stock-pub
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-dev.txt
pip install -e .
cp .env.example .env
```

只使用、不開發時，可以省略 `requirements*.txt`，直接 `pip install .`。

### 2. 啟動

```bash
taiwan-stock web                  # 啟動並自動開啟瀏覽器（預設 http://127.0.0.1:2330）
taiwan-stock web --no-browser     # 不自動開瀏覽器
taiwan-stock web --port 8080      # 換用其他 port
```

`/health` 為健康檢查端點。資料庫預設存放在 `%LOCALAPPDATA%\TaiwanStock\data`（Windows）或 `~/.local/share/taiwan-stock`（macOS / Linux），可用環境變數 `TSL_DATA_DIR` 或 `--data-dir <路徑>` 指定其他位置。資料庫不會進入 Git。

本程式沒有登入機制，只適合在本機使用。若以 `--host` 指定 `0.0.0.0` 或區網 IP，可連到該位址的裝置都能讀取與修改投資資料，啟動時也會印出警告。

### 3. 功能頁面

| 頁面 | 路徑 | 說明 |
| :--- | :--- | :--- |
| 我的持股 | `/` | 均價、市值、佔比、未實現損益、報酬率、當日損益與成本殖利率；展開可看持有批次、含費用回本價、扣息回本價與賣出試算 |
| 商品查詢 | `/securities` | 股票／ETF 查詢；一鍵同步 TWSE／TPEx 名單與上下市狀態，ETF 可補齊保管銀行、配息政策與基本資料 |
| 交易紀錄 | `/transactions` | 逐筆買賣與期初持股，費用依商品與日期自動試算；支援篩選、分頁、CSV 匯出與預覽後匯入 |
| 股票異動 | `/corporate-actions` | 分割／股票股利自動同步並套用，其餘事件手動記錄 |
| 領息月曆 | `/income-calendar` | 依持股試算預計／已知領息金額，可依證券帳戶或領息銀行篩選 |
| 備份與設定 | `/settings` | 本機資料庫備份與還原、費率與手續費折扣、證券帳戶管理 |

### 4. 同步指令

Web 上的同步按鈕會以背景任務執行，可在 `/sync-jobs/<job_id>` 查看進度。CLI 可獨立於 Web 執行，例如另開終端機或掛在排程器上：

```bash
taiwan-stock sync etf-distributions --held-only  # 日常更新持有／追蹤 ETF 的配息
taiwan-stock sync etf-distributions --full       # 全市場 ETF 配息校正（預設近 3 年）
taiwan-stock sync investment-trust               # ETF 官方銀行／配息頻率／政策與基本資料
taiwan-stock sync delistings                     # TWSE／TPEx 終止上市／上櫃狀態
taiwan-stock sync stock-dividends                # 回補個股歷史現金股利，並從 MOPS 補上發放日
taiwan-stock sync job-status <job_id>            # 查詢任務狀態
taiwan-stock sync retry-job <job_id>             # 重試任務未完成的部分
taiwan-stock backup list                         # 列出備份
taiwan-stock backup restore <檔名>               # Web 無法開啟時從終端機還原備份
```

官方來源有請求頻率限制，全市場同步可能需要數十分鐘。若同步途中被限流，會提前停止並印出重試指令，建議稍後再執行。

### 5. 測試

```bash
ruff format --check .
ruff check .
pytest --cov=src --cov-report=term-missing --cov-fail-under=80
pip-audit -r requirements.txt
pip-audit -r requirements-dev.txt
pre-commit run --all-files
```

## License

[MIT License](./LICENSE) © 2026 Wilson Tu
