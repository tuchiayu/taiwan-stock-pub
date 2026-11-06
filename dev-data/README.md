# 開發資料 (Development Data)

從本 repo 的原始碼執行 `taiwan-stock`（例如 `pip install -e .` 後）時，若沒有指定 `--data-dir` 或環境變數 `TSL_DATA_DIR`，資料庫、備份與同步紀錄都放在這個資料夾，不會讀寫安裝版（`pip install .`、`pipx`、`uv tool`）使用的正式資料（Windows `%LOCALAPPDATA%\TaiwanStock\data`、其他系統 `~/.local/share/taiwan-stock`）。

- 這個檔案是開發版的標記，請保留；資料夾內其他檔案都不進版控。
- `taiwan-stock web` 啟動訊息會標示「開發資料」。
- 第一次執行時會建立空的資料庫。

## 放入正式資料的複本

資料庫使用 WAL，最新寫入可能還在 `taiwan_stock.db-wal`，不要直接複製執行中的 `taiwan_stock.db`。

1. 在正式環境的網頁建立備份，或以 SQLite backup API 複製。
2. 把備份檔放到這裡並命名為 `taiwan_stock.db`；`backups/`、`sync-logs/` 不需要複製。
3. 之後在開發版套用的 migration 只會改到這份複本。

需要直接使用正式資料時，以 `--data-dir <路徑>` 或 `TSL_DATA_DIR` 明確指定。
