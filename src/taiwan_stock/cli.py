"""Local Web launcher and foreground clients for durable synchronization jobs."""

import argparse
import json
import sqlite3
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn

from taiwan_stock.catalog import investment_trust_adapters as trust_adapters
from taiwan_stock.catalog import sync_history
from taiwan_stock.infrastructure import backup, migrate, sync_jobs
from taiwan_stock.infrastructure.db import (
    dev_data_dir,
    get_connection,
    resolve_data_dir,
)
from taiwan_stock.main import create_app
from taiwan_stock.shared.http import new_client as new_client
from taiwan_stock.shared.ids import new_id
from taiwan_stock.shared.version import get_version
from taiwan_stock.web.csrf import LOCAL_HOSTS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="taiwan-stock",
        description=(
            "Taiwan Stock — Personal investment portfolio and market research tool."
        ),
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {get_version()}",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    web_parser = subparsers.add_parser("web", help="Start the local Web UI server")
    web_parser.add_argument(
        "--port",
        type=int,
        default=2330,
        help="Port to listen on (default: 2330)",
    )
    web_parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host interface to bind to (default: 127.0.0.1)",
    )
    web_parser.add_argument(
        "--no-browser",
        action="store_true",
        default=False,
        help="Do not open the default browser upon startup",
    )
    web_parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Path to data directory (default: %%LOCALAPPDATA%%\\TaiwanStock\\data)",
    )

    sync_parser = subparsers.add_parser(
        "sync",
        help="Run a long catalog sync job from the terminal instead of the web UI",
    )
    sync_subparsers = sync_parser.add_subparsers(dest="sync_target", required=True)

    etf_dist_parser = sync_subparsers.add_parser(
        "etf-distributions",
        help="Sync TWSE/TPEx ETF distribution history (per-ETF/year, paced — minutes)",
    )
    etf_dist_parser.add_argument("--data-dir", type=Path, default=None)
    etf_dist_parser.add_argument(
        "--years", type=int, default=3, help="Lookback window in years (default: 3)"
    )
    etf_dist_parser.add_argument(
        "--reference-year",
        type=int,
        default=None,
        help="Last year of the lookback window (default: current Taipei year)",
    )
    etf_mode = etf_dist_parser.add_mutually_exclusive_group()
    etf_mode.add_argument(
        "--full",
        action="store_true",
        help="Explicit full-market historical correction (default)",
    )
    etf_mode.add_argument(
        "--held-only",
        action="store_true",
        default=False,
        help="Only traded/watched ETFs' recent years — seconds, not minutes; "
        "ignores --years/--reference-year (design 016)",
    )

    trust_parser = sync_subparsers.add_parser(
        "investment-trust",
        help="Sync ETF custodian bank / distribution frequency from 投信 sources",
    )
    trust_parser.add_argument("--data-dir", type=Path, default=None)
    trust_parser.add_argument(
        "--issuer",
        choices=trust_adapters.ISSUER_NAME_PREFIXES,
        default=None,
        help="Only sync this one 投信 (e.g. re-testing a single source after a fix)",
    )

    dividends_parser = sync_subparsers.add_parser(
        "stock-dividends",
        help="Backfill past cash dividends of traded/watched stocks and fill "
        "their pay dates from MOPS (design 014/015)",
    )
    dividends_parser.add_argument("--data-dir", type=Path, default=None)
    dividends_parser.add_argument(
        "--years",
        type=int,
        default=2,
        help="Lookback for watched-only stocks; traded stocks start at their "
        "first acquisition (default: 2)",
    )

    status_parser = sync_subparsers.add_parser(
        "delistings", help="Sync official TWSE/TPEx termination dates and status"
    )
    status_parser.add_argument("--data-dir", type=Path, default=None)

    scale_parser = sync_subparsers.add_parser(
        "etf-scale",
        help="Sync ETF size (SITCA monthly) and holder counts (TDCC weekly)",
    )
    scale_parser.add_argument(
        "--force",
        action="store_true",
        help="Ask the sources again even within the 6-hour retry interval",
    )
    scale_parser.add_argument("--data-dir", type=Path, default=None)

    retry_parser = sync_subparsers.add_parser(
        "retry",
        help="Retry only the failed/interrupted units of a previous sync run",
    )
    retry_parser.add_argument(
        "run_id", help="Run id, as shown at /catalog-sync-runs/<id>"
    )
    retry_parser.add_argument("--data-dir", type=Path, default=None)

    for command, argument, help_text in (
        ("job-status", "job_id", "Show a durable synchronization job"),
        ("retry-job", "job_id", "Retry unfinished steps/units of a job"),
        (
            "recover-legacy",
            None,
            "Recover old source records while Web and all old syncs are stopped",
        ),
    ):
        target = sync_subparsers.add_parser(command, help=help_text)
        target.add_argument("--data-dir", type=Path, default=None)
        if argument:
            target.add_argument(argument)

    # Design 064: usable while the Web is down because the DB isn't ready.
    backup_parser = subparsers.add_parser(
        "backup", help="List or restore database backups from the terminal"
    )
    backup_subparsers = backup_parser.add_subparsers(
        dest="backup_target", required=True
    )
    backup_list = backup_subparsers.add_parser("list", help="List backups")
    backup_list.add_argument("--data-dir", type=Path, default=None)
    backup_restore = backup_subparsers.add_parser(
        "restore", help="Restore a backup over the current database"
    )
    backup_restore.add_argument("filename", help="Backup filename, from backup list")
    backup_restore.add_argument("--data-dir", type=Path, default=None)
    return parser


def run_web(args: argparse.Namespace) -> None:
    data_dir = args.data_dir or resolve_data_dir()
    app = create_app(data_dir, bind_host=args.host)

    # A wildcard bind is not a name the trusted-host check accepts (#87).
    browse_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    if ":" in browse_host:
        browse_host = f"[{browse_host.strip('[]')}]"
    url = f"http://{browse_host}:{args.port}"
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    # Design 077: say so when this is the source tree's development data.
    label = "，開發資料" if Path(data_dir).resolve() == dev_data_dir() else ""
    print(f"Starting Taiwan Stock at {url} (data: {data_dir}{label})")
    if args.host.strip("[]").lower() not in LOCAL_HOSTS:
        # There is no login: the Host check only stops browsers, not a
        # client that sends "Host: localhost" itself (design 030).
        print(
            f"警告：本程式沒有登入保護；綁定 {args.host} 後，"
            "可連到此位址的裝置都能讀取與修改投資資料、還原備份。",
            file=sys.stderr,
        )
    uvicorn.run(app, host=args.host, port=args.port)


def _open_conn(data_dir: Path | None):
    conn = get_connection(data_dir or resolve_data_dir())
    try:
        migrate.apply_pending(conn)
        migrate.validate_runtime_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _print_result(conn, label: str, result: dict) -> None:
    print(
        f"{label} — run_id={result['run_id']}: {json.dumps(result, ensure_ascii=False)}"
    )
    report = sync_history.get_report(conn, result["run_id"])
    pending = report["counts"].get("pending", 0) if report else 0
    if pending:
        # The circuit breaker (see sync.py's module docstring) stopped
        # early rather than grinding through a source that's blocking
        # us — live observation shows an immediate retry doesn't help
        # (a block outlasts our own retry backoff by minutes), so this
        # is a suggestion to come back later, not an automatic wait: we
        # don't reliably know how long "later" needs to be, and a CLI
        # silently sleeping on a guess is worse than just telling you.
        print(
            f"  {label}：{pending} 筆疑似遇到來源限流而提前停止（非個別商品失敗），"
            f"立即重試通常沒有用；建議稍後執行 "
            f"taiwan-stock sync retry {result['run_id']} 再試一次。"
        )


def run_sync_job(args):
    data_dir = resolve_data_dir(args.data_dir)
    if args.sync_target == "recover-legacy":
        import sqlite3
        from contextlib import closing

        from taiwan_stock.infrastructure.db import (
            DB_FILENAME,
            exclusive_database_access,
            write_transaction,
        )

        try:
            with (
                exclusive_database_access(data_dir),
                closing(
                    sqlite3.connect(data_dir / DB_FILENAME, isolation_level=None)
                ) as conn,
            ):
                conn.row_factory = sqlite3.Row
                migrate.apply_pending(conn)
                # Exclusive connection coordination proves all cooperating old
                # Web/CLI connections have stopped; never do this at Web startup.
                with write_transaction(conn):
                    sync_history.recover_interrupted(conn)
            print("舊版未完成同步紀錄已標示中斷")
            return 0
        except RuntimeError as exc:
            print(f"錯誤：{exc}", file=sys.stderr)
            return 1
    conn = _open_conn(data_dir)
    job = None
    submission_key = new_id()
    try:
        if args.sync_target == "job-status":
            sync_jobs.recover(conn, data_dir)
            job = sync_jobs.get(conn, args.job_id)
            if job is None:
                raise ValueError("找不到任務")
            print(json.dumps(job, ensure_ascii=False, indent=2))
            return 0
        if args.sync_target == "retry-job":
            job = sync_jobs.retry(
                conn, data_dir, args.job_id, origin="cli", request_key=submission_key
            )
        else:
            operation = args.sync_target
            options = {}
            if operation == "etf-distributions":
                operation = (
                    "held-etf-distributions"
                    if args.held_only
                    else "full-etf-distributions"
                )
                if not args.held_only:
                    from taiwan_stock.shared.dates import today_taipei

                    options = {
                        "years": args.years,
                        "reference_year": args.reference_year
                        or int(today_taipei()[:4]),
                    }
                    count = conn.execute(
                        "SELECT count(*) FROM securities WHERE "
                        "security_type='etf' AND market IN ('TWSE','TPEx')"
                    ).fetchone()[0]
                    print(
                        f"全市場 ETF 校正：{count} 檔 × {args.years} 年，"
                        f"約 {count * args.years} 個單位，可能需要數十分鐘"
                    )
            elif operation == "investment-trust":
                options = {"issuer": args.issuer}
            elif operation == "stock-dividends":
                options = {"years": args.years}
            elif operation == "etf-scale":
                options = {"force": args.force}
            elif operation == "retry":
                options = {"run_id": args.run_id}
            job = sync_jobs.submit(
                conn,
                data_dir,
                operation,
                options,
                origin="cli",
                request_key=submission_key,
            )
        print(f"job_id={job['id']}；進度頁 /sync-jobs/{job['id']}")
        try:
            job = sync_jobs.wait(
                conn,
                data_dir,
                job["id"],
                on_progress=lambda j: print(
                    f"{j['phase']}：已處理 {j['processed']}，狀態 {j['status']}"
                ),
            )
        except KeyboardInterrupt:
            sync_jobs.request_cancel(conn, job["id"])
            print("正在中止同步，等待目前請求／單位結束…")
            sync_jobs.wait(conn, data_dir, job["id"])
            return 130
        result = job["result"]
        if result is not None:
            if job["operation"] in ("held-etf-distributions", "full-etf-distributions"):
                for market, label in (
                    ("twse", "TWSE ETF 配息"),
                    ("tpex", "TPEx ETF 配息"),
                ):
                    if market in result and result[market].get("run_id"):
                        _print_result(conn, label, result[market])
                if "pending_amounts" in result:
                    print(f"仍待公布金額：{result['pending_amounts']} 筆")
            elif result.get("run_id"):
                _print_result(conn, job["phase"], result)
            else:
                print(
                    "個股歷史現金股利／發放日："
                    f"{json.dumps(result, ensure_ascii=False)}"
                )
        if job["error"]:
            print(f"錯誤：{job['error']}", file=sys.stderr)
        # Preserve existing full-market/trust/source-retry exit semantics:
        # recorded per-unit failures still print diagnostics and return 0.
        if (
            args.sync_target != "retry-job"
            and job["status"] == "partial"
            and job["operation"]
            in ("full-etf-distributions", "investment-trust", "retry")
        ):
            return 0
        return 0 if job["status"] == "success" else 1
    except KeyboardInterrupt:
        if job is None:
            row = conn.execute(
                "SELECT id FROM sync_jobs WHERE request_key=?", (submission_key,)
            ).fetchone()
            job = sync_jobs.get(conn, row[0]) if row else None
        if job:
            sync_jobs.request_cancel(conn, job["id"])
            sync_jobs.wait(conn, data_dir, job["id"])
        return 130
    except (sync_history.SyncInProgress, ValueError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def run_backup(args) -> int:
    """No `_open_conn`: that would migrate and refuse a DB that isn't ready."""
    data_dir = resolve_data_dir(args.data_dir)
    if args.backup_target == "list":
        items = backup.list_backups(data_dir)
        if not items:
            print("尚無備份")
        for item in items:
            print(
                "  ".join(
                    filter(
                        None,
                        [
                            item["filename"],
                            item["modified_at_taipei"],
                            item["kind_label"],
                            item["note"],
                        ],
                    )
                )
            )
        return 0
    try:
        safety = backup.restore_backup(data_dir, args.filename)
    except (backup.BackupError, migrate.DatabaseNotReadyError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    print(f"已還原 {args.filename}；還原前的資料已保存在 {safety}")
    print("Web 若正在執行，請重新啟動")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if args.command == "web":
        run_web(args)
        return 0

    if args.command == "sync":
        try:
            return run_sync_job(args)
        except (RuntimeError, sqlite3.DatabaseError) as exc:
            print(f"資料庫或同步作業失敗：{exc}", file=sys.stderr)
            return 1

    if args.command == "backup":
        return run_backup(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
