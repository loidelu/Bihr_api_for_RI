"""Command-line entry point for the BIHR connector.

Usage:
    python -m bihr_connector.cli sync-catalogs --catalog-type Products
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bihr_connector.bihr_client import BihrApiError, BihrClient
from bihr_connector.brand_dictionary import sync_brand_dictionary
from bihr_connector.brand_match import (
    compute_brand_match,
    find_possible_matches,
    upsert_brand_match_candidates,
)
from bihr_connector.catalog_sync import CatalogType, sync_catalog
from bihr_connector.config import get_settings
from bihr_connector.db import get_session
from bihr_connector.scheduler import DEFAULT_SLEEP_SECONDS, activate_brand, main_loop, run_cycle

CATALOG_TYPES: tuple[CatalogType, ...] = ("Products", "Prices", "Stocks", "SalesCategories")


def _cmd_sync_catalogs(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.BIHR_USERNAME or not settings.BIHR_PASSWORD:
        print("BIHR_USERNAME/BIHR_PASSWORD are not set (check your .env)", file=sys.stderr)
        return 2

    with BihrClient(settings.BIHR_BASE_URL, settings.BIHR_USERNAME, settings.BIHR_PASSWORD) as client:
        with get_session() as session:
            try:
                run = sync_catalog(session, client, args.catalog_type, mode="full")
            except BihrApiError as exc:
                print(f"BIHR API error syncing {args.catalog_type}: {exc}", file=sys.stderr)
                return 1
            except ValueError as exc:
                print(f"Catalog data error syncing {args.catalog_type}: {exc}", file=sys.stderr)
                return 1

    print(
        f"{run.catalog_type}: status={run.status} "
        f"rows_raw={run.rows_raw} rows_unique={run.rows_unique} "
        f"new={run.rows_new} modified={run.rows_modified} unchanged={run.rows_unchanged}"
    )
    return 0


def _cmd_sync_brands(args: argparse.Namespace) -> int:
    """Derive brand dictionary from products catalog."""
    with get_session() as session:
        try:
            result = sync_brand_dictionary(session)
        except Exception as exc:
            print(f"Error syncing brand dictionary: {exc}", file=sys.stderr)
            return 1
        session.commit()

    print(
        f"Brands: new={result['new']} renamed={result['renamed']} "
        f"unchanged={result['unchanged']} skipped={result['skipped']}"
    )
    return 0


def _cmd_match_brands(args: argparse.Namespace) -> int:
    """Cross-reference BIHR brands against Reference Inteligencia's Master
    and upsert the results into brand_match_candidates."""
    settings = get_settings()
    if not settings.RI_DATABASE_URL:
        print(
            "RI_DATABASE_URL is not set (check your .env) -- the read-only "
            "role must be provisioned on Reference Inteligencia's database "
            "first; see Task 6 brief, Step 0",
            file=sys.stderr,
        )
        return 2

    ri_engine = create_engine(settings.RI_DATABASE_URL, echo=False)
    RiSessionLocal = sessionmaker(bind=ri_engine, expire_on_commit=False)
    ri_session = RiSessionLocal()

    try:
        with get_session() as bihr_session:
            candidates, ambiguous = compute_brand_match(bihr_session, ri_session)
            upsert_brand_match_candidates(bihr_session, candidates)
            bihr_session.commit()
            # Read-only and purely informational -- never persisted, never
            # changes which brands count as matched. See PossibleMatch's
            # docstring: exact-match stays the only thing that's "decided".
            hints = find_possible_matches(bihr_session, ri_session, candidates)
    finally:
        ri_session.close()
        ri_engine.dispose()

    hints_by_brand: dict[str, list] = {}
    for hint in hints:
        hints_by_brand.setdefault(hint.brand_code, []).append(hint)

    if args.print_report:
        for candidate in sorted(
            candidates, key=lambda c: c.master_product_count, reverse=True
        ):
            print(
                f"{candidate.brand_code}\t"
                f"{candidate.matched_manufacturer_name or '(no match)'}\t"
                f"master_product_count={candidate.master_product_count}"
            )
            for hint in hints_by_brand.get(candidate.brand_code, []):
                print(f"\tposible: '{hint.label}' -> {hint.manufacturer_name} (id={hint.manufacturer_id})")
    else:
        matched = sum(1 for c in candidates if c.matched_manufacturer_id is not None)
        print(f"Brand match: {matched}/{len(candidates)} brands matched in Reference Inteligencia")
        if hints_by_brand:
            print(f"  + {len(hints_by_brand)} unmatched brand(s) have a possible name-variant hint (--print-report to see them)")

    if ambiguous:
        print(
            f"WARNING: {len(ambiguous)} brand(s) have ambiguous aliases in "
            "Reference Inteligencia (more than one manufacturer_id for the "
            "same brand_code) and were reported as unmatched -- needs manual "
            "cleanup on the RI side:",
            file=sys.stderr,
        )
        for alias in ambiguous:
            print(
                f"  brand_code={alias.brand_code} manufacturer_ids={alias.manufacturer_ids}",
                file=sys.stderr,
            )

    return 0


def _cmd_activate(args: argparse.Namespace) -> int:
    """Mark a BIHR brand active so future incremental-refresh cycles
    (Task 8's scheduler loop) pick it up."""
    with get_session() as session:
        try:
            activate_brand(session, args.brand_code)
        except ValueError as exc:
            print(f"Error activating brand {args.brand_code!r}: {exc}", file=sys.stderr)
            return 1

    print(f"Brand {args.brand_code!r} activated")
    return 0


def _cmd_run_scheduler(args: argparse.Namespace) -> int:
    """Run the scheduler loop: due bulk catalogs + every active brand's
    incremental refresh, forever (the container's entrypoint) -- or just
    once with `--once`, for manual verification and for the Docker
    build-verification step."""
    settings = get_settings()
    if not settings.BIHR_USERNAME or not settings.BIHR_PASSWORD:
        print("BIHR_USERNAME/BIHR_PASSWORD are not set (check your .env)", file=sys.stderr)
        return 2

    if not args.once:
        main_loop(sleep_seconds=args.sleep_seconds)
        return 0  # unreachable in practice -- main_loop runs forever

    with BihrClient(settings.BIHR_BASE_URL, settings.BIHR_USERNAME, settings.BIHR_PASSWORD) as client:
        with get_session() as session:
            try:
                runs = run_cycle(session, client, now=datetime.now(timezone.utc))
            except BihrApiError as exc:
                print(f"BIHR API error running scheduler cycle: {exc}", file=sys.stderr)
                return 1

    if not runs:
        print("Scheduler cycle: nothing due (no catalogs due, no active brands)")
    for run in runs:
        print(
            f"{run.catalog_type} ({run.mode}): status={run.status} "
            f"new={run.rows_new} modified={run.rows_modified} unchanged={run.rows_unchanged}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bihr-connector", description="BIHR API connector CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser(
        "sync-catalogs", help="Download and upsert one full BIHR bulk catalog"
    )
    sync_parser.add_argument(
        "--catalog-type",
        required=True,
        choices=CATALOG_TYPES,
        help="Which bulk catalog to sync",
    )
    sync_parser.set_defaults(func=_cmd_sync_catalogs)

    brands_parser = subparsers.add_parser(
        "sync-brands", help="Derive brand dictionary from products catalog"
    )
    brands_parser.set_defaults(func=_cmd_sync_brands)

    match_parser = subparsers.add_parser(
        "match-brands",
        help="Cross-reference BIHR brands against Reference Inteligencia's Master",
    )
    match_parser.add_argument(
        "--print-report",
        action="store_true",
        help="Print every brand sorted descending by master_product_count",
    )
    match_parser.set_defaults(func=_cmd_match_brands)

    activate_parser = subparsers.add_parser(
        "activate",
        help="Mark a BIHR brand active for incremental refresh",
    )
    activate_parser.add_argument(
        "--brand-code",
        required=True,
        help="BIHR brand_code to activate (see match-brands --print-report)",
    )
    activate_parser.set_defaults(func=_cmd_activate)

    scheduler_parser = subparsers.add_parser(
        "run-scheduler",
        help="Run the scheduler loop (due bulk catalogs + active-brand refresh), forever or once",
    )
    scheduler_parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single cycle and exit, instead of looping forever (the container's normal mode)",
    )
    scheduler_parser.add_argument(
        "--sleep-seconds",
        type=int,
        default=DEFAULT_SLEEP_SECONDS,
        help=f"Seconds to sleep between cycles when looping forever (default: {DEFAULT_SLEEP_SECONDS})",
    )
    scheduler_parser.set_defaults(func=_cmd_run_scheduler)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
