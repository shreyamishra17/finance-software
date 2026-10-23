from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bank_fetcher import _discover_bank_keys, _load_fetcher_config, fetch_statements, DEFAULT_RAW_DIR
from bank_fetcher.credential_manager import delete_credentials, setup_credentials


def cmd_scaffold(args: argparse.Namespace) -> int:
    from bank_fetcher.config_scaffold import scaffold_bank_config
    scaffold_bank_config(bank_key=args.bank, login_url=args.login_url)
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    banks = _discover_bank_keys() if args.bank == "all" else [args.bank]
    for bank_key in banks:
        cfg = _load_fetcher_config(bank_key)
        print(f"\n--- Setting up credentials for: {cfg['bank_display_name']} ---")
        setup_credentials(bank_key=bank_key, fetcher_cfg=cfg)
    return 0


def cmd_delete_creds(args: argparse.Namespace) -> int:
    cfg = _load_fetcher_config(args.bank)
    delete_credentials(bank_key=args.bank, fetcher_cfg=cfg)
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    raw_dir = Path(args.raw_dir) if args.raw_dir else DEFAULT_RAW_DIR
    banks = _discover_bank_keys() if args.bank == "all" else [args.bank]
    overall_exit = 0

    for bank_key in banks:
        print(f"\n[{bank_key}] Fetching {args.months} month(s)...")
        fetch_result, pipeline_results = fetch_statements(
            bank_key=bank_key,
            months=args.months,
            raw_dir=raw_dir,
            run_pipeline=not args.no_parse,
            username=args.username,
        )

        print(f"[{bank_key}] Downloaded: {len(fetch_result.downloaded)} PDF(s)")
        for p in fetch_result.downloaded:
            print(f"  -> {p}")
        if fetch_result.skipped:
            print(f"[{bank_key}] Skipped (already present): {len(fetch_result.skipped)}")
        if fetch_result.errors:
            for e in fetch_result.errors:
                print(f"  ERROR: {e}", file=sys.stderr)
            overall_exit = 1

        if pipeline_results:
            ok = sum(1 for r in pipeline_results if r.success)
            print(f"[{bank_key}] Parsed: {ok}/{len(pipeline_results)} PDF(s)")
            for r in pipeline_results:
                if r.error:
                    print(f"  PARSE ERROR ({r.pdf_path.name}): {r.error}", file=sys.stderr)
                    overall_exit = 1
                elif r.success:
                    print(f"  -> {r.transaction_count} transactions -> {r.account_csv_path}")

    return overall_exit


def main() -> int:
    banks = _discover_bank_keys()
    bank_choices = banks + ["all"]

    parser = argparse.ArgumentParser(
        prog="python -m bank_fetcher",
        description="FIFO bank statement fetcher — downloads PDFs via browser automation.",
        epilog=f"Configured banks: {', '.join(banks) or 'none — add a config to bank_fetcher/configs/'}",
    )
    sub = parser.add_subparsers(dest="command")

    # scaffold subcommand
    p_scaffold = sub.add_parser(
        "scaffold",
        help="Auto-generate a bank config by recording your login flow (uses playwright codegen)"
    )
    p_scaffold.add_argument("--bank", required=True,
                            help="Short key for this bank, e.g. 'chase' (used in filenames and --bank flag)")
    p_scaffold.add_argument("--login-url", required=True,
                            help="URL of the bank's login page, e.g. https://www.chase.com/")

    # setup subcommand
    p_setup = sub.add_parser("setup", help="Store credentials in OS keychain (run once per bank)")
    p_setup.add_argument("--bank", required=True, choices=bank_choices)

    # delete-creds subcommand
    p_del = sub.add_parser("delete-creds", help="Remove stored credentials from OS keychain")
    p_del.add_argument("--bank", required=True, choices=banks)

    # fetch flags (top-level)
    parser.add_argument("--bank", choices=bank_choices, default=None)
    parser.add_argument("--months", type=int, default=3,
                        help="Number of statement months to download (default: 3)")
    parser.add_argument("--no-parse", action="store_true",
                        help="Download only; skip parse pipeline")
    parser.add_argument("--raw-dir", type=str, default=None,
                        help="Override destination directory for raw PDFs")
    parser.add_argument("--username", type=str, default="shreya_mishra",
                        help="Username slug for persistence paths")

    args = parser.parse_args()

    if args.command == "scaffold":
        return cmd_scaffold(args)
    if args.command == "setup":
        return cmd_setup(args)
    if args.command == "delete-creds":
        return cmd_delete_creds(args)
    if not args.bank:
        parser.print_help()
        return 1
    return cmd_fetch(args)


if __name__ == "__main__":
    raise SystemExit(main())
