"""Cross-reference BIHR brands against Reference Inteligencia's Master.

Reference Inteligencia ("RI") is a separate, already-existing production
application outside this repo. The brief (ADR 0005) lists four of its
tables under the ``ri`` schema, three of which this module queries:

- ``core_manufacturers(id, name)``
- ``source_brand_labels(brand_code, manufacturer_id)`` -- the alias table
  mapping a brand name/code variant to a manufacturer. Lookup is always an
  exact alias match on ``brand_code``, never fuzzy string similarity, to
  stay consistent with the rest of the codebase's identity rules.
- ``master_products(manufacturer_id)`` -- counted per manufacturer to show
  how much RI coverage a brand already has.

The fourth table in the brief, ``source_brand_coverage``, is deliberately
*not* queried here: it plays no role in name-to-manufacturer resolution,
it's only present in the Step 0 grant list (the read-only role needs
SELECT on it for some other RI-side consumer), so this function has no
query dependency on it.

``compute_brand_match`` takes two SQLAlchemy ``Session`` objects and is
deliberately agnostic about whether they point at the same database or two
different ones: it only issues plain SELECTs through each, never joins
across them in a single query. All cross-referencing happens in Python
after both sides have been read independently.
"""

from datetime import datetime, timezone

from sqlalchemy import MetaData, Table, func, select
from sqlalchemy.orm import Session

from bihr_connector.models import Brand, BrandMatchCandidate


class AmbiguousBrandAliasError(Exception):
    """Raised when a brand_code resolves to more than one distinct
    manufacturer_id in RI's source_brand_labels.

    The brief's own identity rule is "exact alias lookup only, never
    fuzzy" -- silently picking one of several conflicting alias rows would
    undercut that guarantee by making the result depend on arbitrary row
    order. We have no verified column on the real source_brand_labels
    table (e.g. a created/updated timestamp) to justify a deterministic
    tie-break via ORDER BY, so ambiguity is surfaced loudly instead of
    silently resolved. Whoever owns the RI alias data decides the real fix
    (dedupe the aliases, or add a tie-break column) -- this function will
    not guess.
    """

    def __init__(self, brand_code: str, manufacturer_ids: list[int]) -> None:
        self.brand_code = brand_code
        self.manufacturer_ids = manufacturer_ids
        super().__init__(
            f"brand_code {brand_code!r} has {len(manufacturer_ids)} distinct "
            f"manufacturer_id aliases in source_brand_labels: {manufacturer_ids} "
            "-- ambiguous, refusing to silently pick one"
        )


def _ri_table(ri_session: Session, name: str, columns: tuple[str, ...]) -> Table:
    """Reflect a single RI table by name from ri_session's bound engine.

    Reflecting per-call (rather than caching module-level Table objects)
    keeps compute_brand_match agnostic about which engine/database
    ri_session is bound to -- it may differ between calls (e.g. tests vs.
    production) and reflection must follow the session, not a fixed engine.
    """
    metadata = MetaData()
    table = Table(name, metadata, autoload_with=ri_session.get_bind())
    missing = [c for c in columns if c not in table.columns]
    if missing:
        raise ValueError(f"RI table {name!r} is missing expected column(s): {missing}")
    return table


def compute_brand_match(
    bihr_session: Session, ri_session: Session
) -> list[BrandMatchCandidate]:
    """For every row in ``brands``, look up a matching RI manufacturer by
    exact alias (via ``source_brand_labels.brand_code``), count
    ``master_products`` rows for that manufacturer, and return one
    ``BrandMatchCandidate`` per brand.

    Brands with no matching alias are reported with
    ``matched_manufacturer_id=None`` and ``master_product_count=0`` --
    never dropped, never raised as an error.

    Brands whose brand_code aliases more than one distinct
    manufacturer_id raise ``AmbiguousBrandAliasError`` instead of silently
    picking one -- see that class's docstring for why.

    This function only reads; it does not add the returned candidates to
    any session or commit anything. Callers decide how to persist them.
    """
    core_manufacturers = _ri_table(ri_session, "core_manufacturers", ("id", "name"))
    source_brand_labels = _ri_table(
        ri_session, "source_brand_labels", ("brand_code", "manufacturer_id")
    )
    master_products = _ri_table(ri_session, "master_products", ("manufacturer_id",))

    brands = bihr_session.execute(select(Brand.brand_code)).scalars().all()

    now = datetime.now(timezone.utc)
    candidates: list[BrandMatchCandidate] = []

    for brand_code in brands:
        alias_manufacturer_ids = sorted(
            {
                row[0]
                for row in ri_session.execute(
                    select(source_brand_labels.c.manufacturer_id).where(
                        source_brand_labels.c.brand_code == brand_code
                    )
                ).all()
            }
        )

        if len(alias_manufacturer_ids) > 1:
            raise AmbiguousBrandAliasError(brand_code, alias_manufacturer_ids)

        matched_manufacturer_id: int | None = None
        matched_manufacturer_name: str | None = None
        master_product_count = 0

        if alias_manufacturer_ids:
            manufacturer_id = alias_manufacturer_ids[0]
            manufacturer_row = ri_session.execute(
                select(core_manufacturers.c.name).where(
                    core_manufacturers.c.id == manufacturer_id
                )
            ).first()
            if manufacturer_row is not None:
                matched_manufacturer_id = manufacturer_id
                matched_manufacturer_name = manufacturer_row[0]
                master_product_count = _count_master_products(
                    ri_session, master_products, manufacturer_id
                )

        candidates.append(
            BrandMatchCandidate(
                brand_code=brand_code,
                matched_manufacturer_id=matched_manufacturer_id,
                matched_manufacturer_name=matched_manufacturer_name,
                master_product_count=master_product_count,
                computed_at=now,
            )
        )

    return candidates


def _count_master_products(ri_session: Session, master_products: Table, manufacturer_id: int) -> int:
    return ri_session.execute(
        select(func.count()).select_from(master_products).where(
            master_products.c.manufacturer_id == manufacturer_id
        )
    ).scalar_one()


def upsert_brand_match_candidates(
    bihr_session: Session, candidates: list[BrandMatchCandidate]
) -> None:
    """Persist computed candidates into brand_match_candidates, one row per
    brand_code, updating in place on repeat runs rather than duplicating.

    Does not commit -- callers control the transaction boundary, same
    convention as sync_brand_dictionary.
    """
    for candidate in candidates:
        existing = bihr_session.get(BrandMatchCandidate, candidate.brand_code)
        if existing is None:
            bihr_session.add(candidate)
        else:
            existing.matched_manufacturer_id = candidate.matched_manufacturer_id
            existing.matched_manufacturer_name = candidate.matched_manufacturer_name
            existing.master_product_count = candidate.master_product_count
            existing.computed_at = candidate.computed_at

    bihr_session.flush()
