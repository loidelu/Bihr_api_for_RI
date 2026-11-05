"""Command-line entry point for the BIHR connector.

Usage:
    python -m bihr_connector.cli sync-catalogs --catalog-type Products
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bihr_connector.bihr_client import BihrApiError, BihrClient
from bihr_connector.brand_dictionary import sync_brand_dictionary
from bihr_connector.brand_match import compute_brand_match, upsert_brand_match_candidates
from bihr_connector.catalog_sync import CatalogType, sync_catalog
from bihr_connector.config import get_settings
from bihr_connector.db import get_session
from bihr_connector.scheduler import activate_brand

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
    finally:
        ri_session.close()
        ri_engine.dispose()

    if args.print_report:
        for candidate in sorted(
            candidates, key=lambda c: c.master_product_count, reverse=True
        ):
            print(
                f"{candidate.brand_code}\t"
                f"{candidate.matched_manufacturer_name or '(no match)'}\t"
                f"master_product_count={candidate.master_product_count}"
            )
    else:
        matched = sum(1 for c in candidates if c.matched_manufacturer_id is not None)
        print(f"Brand match: {matched}/{len(candidates)} brands matched in Reference Inteligencia")

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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
