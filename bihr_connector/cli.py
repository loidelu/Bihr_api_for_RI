"""Command-line entry point for the BIHR connector.

Usage:
    python -m bihr_connector.cli sync-catalogs --catalog-type Products
"""

from __future__ import annotations

import argparse
import sys

from bihr_connector.bihr_client import BihrApiError, BihrClient
from bihr_connector.brand_dictionary import sync_brand_dictionary
from bihr_connector.catalog_sync import CatalogType, sync_catalog
from bihr_connector.config import get_settings
from bihr_connector.db import get_session

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
        f"Brands: new={result['new']} renamed={result['renamed']} unchanged={result['unchanged']}"
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
