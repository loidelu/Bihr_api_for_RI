"""Cross-reference BIHR brands against Reference Inteligencia's Master.

Reference Inteligencia ("RI") is a separate, already-existing production
application outside this repo. The brief (ADR 0005) lists four of its
tables under the ``ri`` schema (configurable via ``settings.RI_SCHEMA``,
see ``bihr_connector/config.py``), three of which this module queries.
The real schema (confirmed against production on 2026-11-06, after an
earlier assumed shape turned out wrong -- see below) is:

- ``core_manufacturers(id, canonical_name, active, website, notes,
  updated_at)``.
- ``source_brand_labels(id, source_system, source_code, label,
  manufacturer_id)`` -- the alias table mapping a brand *name* spelling
  to a manufacturer, shared across every source RI already ingests (e.g.
  ``source_system='parts_europe'``). ``source_code`` identifies the
  source's feed, not a per-brand code -- it is NOT what resolves a brand;
  ``label`` (the brand name text, e.g. ``"BARKBUSTERS"``) is. Lookup is
  always an exact (case-insensitive) match on ``label`` against BIHR's
  ``brand_name``, never fuzzy string similarity, to stay consistent with
  the rest of the codebase's identity rules. Matching by name text rather
  than a BIHR-specific code means this works today even though BIHR has
  no ``source_system`` of its own registered in RI yet -- ``label``
  values are shared brand-name spellings, not source-scoped codes.
- ``master_products(brand_id)`` -- a foreign key to
  ``core_manufacturers.id`` (``fk_master_products_brand``), counted per
  manufacturer to show how much RI coverage a brand already has. Despite
  the name, this is NOT called ``manufacturer_id`` on this table.

Two separate wrong guesses have been made and fixed here, both caught
only by a live run against production, never by local tests (whose
stand-in schema mirrored whichever guess was current):
1. An earlier version assumed ``core_manufacturers(id, name)`` and
   ``source_brand_labels(brand_code, manufacturer_id)``, which crashed
   with ``ValueError: RI table 'core_manufacturers' is missing expected
   column(s): ['name']``.
2. After fixing (1), a second guess assumed ``master_products`` used
   ``manufacturer_id`` (matching the column name on
   ``source_brand_labels``) when it actually uses ``brand_id`` --
   crashed with ``ValueError: RI table 'master_products' is missing
   expected column(s): ['manufacturer_id']``.

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

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import MetaData, Table, func, select
from sqlalchemy.orm import Session

from bihr_connector.config import get_settings
from bihr_connector.models import Brand, BrandMatchCandidate


@dataclass(frozen=True)
class AmbiguousBrandAlias:
    """One BIHR brand whose ``brand_name`` matches ``label`` rows pointing
    at more than one distinct ``manufacturer_id`` in RI's
    source_brand_labels -- a data-quality conflict on the RI side (e.g. an
    uncleaned historical rename), not something this connector can
    resolve on its own.

    The brief's own identity rule is "exact alias lookup only, never
    fuzzy" -- silently picking one of several conflicting alias rows would
    undercut that guarantee by making the result depend on arbitrary row
    order. We have no verified column on the real source_brand_labels
    table (e.g. a created/updated timestamp) to justify a deterministic
    tie-break via ORDER BY, so ambiguity is surfaced as data for the
    caller to act on, instead of being silently resolved -- but it must
    not abort the rest of the report: one dirty row in RI is exactly the
    kind of thing this report exists to surface, not hide behind a crash.
    """

    brand_code: str
    manufacturer_ids: list[int]


def _ri_table(
    ri_session: Session, name: str, columns: tuple[str, ...], schema: str
) -> Table:
    """Reflect a single RI table by name (and schema) from ri_session's
    bound engine.

    Reflecting per-call (rather than caching module-level Table objects)
    keeps compute_brand_match agnostic about which engine/database
    ri_session is bound to -- it may differ between calls (e.g. tests vs.
    production) and reflection must follow the session, not a fixed engine.

    ``schema`` must be passed explicitly (production RI tables live under
    the ``ri`` schema, per ADR 0005, not the connection's default
    ``public`` schema) -- reflecting by bare name would silently resolve
    against the wrong schema whenever one happens to exist there too.
    """
    metadata = MetaData()
    table = Table(name, metadata, schema=schema, autoload_with=ri_session.get_bind())
    missing = [c for c in columns if c not in table.columns]
    if missing:
        raise ValueError(f"RI table {name!r} is missing expected column(s): {missing}")
    return table


def compute_brand_match(
    bihr_session: Session, ri_session: Session, ri_schema: str | None = None
) -> tuple[list[BrandMatchCandidate], list[AmbiguousBrandAlias]]:
    """For every row in ``brands``, look up a matching RI manufacturer by
    exact (case-insensitive) match of ``brand_name`` against
    ``source_brand_labels.label``, count ``master_products`` rows for
    that manufacturer, and return one ``BrandMatchCandidate`` per brand,
    plus a separate list flagging any brand whose label was ambiguous.

    Brands with no matching alias (or an ambiguous one -- see below) are
    reported with ``matched_manufacturer_id=None`` and
    ``master_product_count=0`` -- never dropped, never allowed to error
    out the rest of the batch.

    Brands whose brand_code aliases more than one distinct
    manufacturer_id are reported as unmatched in the returned candidates
    list *and* appended to the second return value as an
    ``AmbiguousBrandAlias`` -- see that class's docstring for why we don't
    try to pick one. Every other brand in the same run is still processed
    normally: one dirty alias row in RI must never prevent the rest of the
    brands from being reported.

    ``ri_schema`` is the schema RI's tables live in on ``ri_session``'s
    database. Defaults to ``settings.RI_SCHEMA`` (``"ri"`` in production,
    per ADR 0005) when not given explicitly -- callers such as tests may
    override it to point at a different schema.

    This function only reads; it does not add the returned candidates to
    any session or commit anything. Callers decide how to persist them.
    """
    schema = ri_schema if ri_schema is not None else get_settings().RI_SCHEMA
    core_manufacturers = _ri_table(ri_session, "core_manufacturers", ("id", "canonical_name"), schema)
    source_brand_labels = _ri_table(
        ri_session, "source_brand_labels", ("label", "manufacturer_id"), schema
    )
    master_products = _ri_table(ri_session, "master_products", ("brand_id",), schema)

    brands = bihr_session.execute(select(Brand.brand_code, Brand.brand_name)).all()

    now = datetime.now(timezone.utc)
    candidates: list[BrandMatchCandidate] = []
    ambiguous: list[AmbiguousBrandAlias] = []

    for brand_code, brand_name in brands:
        alias_manufacturer_ids = sorted(
            {
                row[0]
                for row in ri_session.execute(
                    select(source_brand_labels.c.manufacturer_id).where(
                        func.upper(source_brand_labels.c.label) == func.upper(brand_name)
                    )
                ).all()
            }
        )

        matched_manufacturer_id: int | None = None
        matched_manufacturer_name: str | None = None
        master_product_count = 0

        if len(alias_manufacturer_ids) > 1:
            ambiguous.append(AmbiguousBrandAlias(brand_code, alias_manufacturer_ids))
        elif alias_manufacturer_ids:
            manufacturer_id = alias_manufacturer_ids[0]
            manufacturer_row = ri_session.execute(
                select(core_manufacturers.c.canonical_name).where(
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

    return candidates, ambiguous


@dataclass(frozen=True)
class PossibleMatch:
    """A non-exact textual hint for a BIHR brand that has no exact
    ``label`` match -- e.g. BIHR's "GILLES" vs RI's label "GILLES
    TOOLING", or "FRENKIT" vs "FRENKIT S.L.". These are real, legitimate
    brand-name variants that an exact match deliberately does not resolve
    (per this module's "never fuzzy" rule) -- surfacing them here is
    purely to save a human from re-reading all unmatched brand names by
    hand; it is never auto-selected, never written to
    BrandMatchCandidate, and never counts as a decided match.

    The only heuristic used is case-insensitive substring containment
    (one name is fully contained in the other) -- not edit-distance or
    any scored fuzzy similarity. Containment is a narrow, auditable
    signal: a human reading "GILLES" / "GILLES TOOLING" side by side can
    immediately see why it was suggested, which a similarity score alone
    would not make obvious.
    """

    brand_code: str
    manufacturer_id: int
    manufacturer_name: str
    label: str


def find_possible_matches(
    bihr_session: Session,
    ri_session: Session,
    candidates: list[BrandMatchCandidate],
    ri_schema: str | None = None,
) -> list[PossibleMatch]:
    """For every brand in ``candidates`` with no exact match
    (``matched_manufacturer_id is None``), look for RI labels that
    case-insensitively contain the brand's name or vice versa, and return
    one ``PossibleMatch`` per (brand, label) hit.

    Purely informational -- see ``PossibleMatch`` -- and purely additive
    on top of ``compute_brand_match``: it never changes which brands
    count as matched, never raises on ambiguity (multiple hints for the
    same brand are all returned), and this function itself never writes
    anything to any session.
    """
    schema = ri_schema if ri_schema is not None else get_settings().RI_SCHEMA
    core_manufacturers = _ri_table(ri_session, "core_manufacturers", ("id", "canonical_name"), schema)
    source_brand_labels = _ri_table(
        ri_session, "source_brand_labels", ("label", "manufacturer_id"), schema
    )

    unmatched = {c.brand_code: c for c in candidates if c.matched_manufacturer_id is None}
    if not unmatched:
        return []

    brand_names = {
        row[0]: row[1]
        for row in bihr_session.execute(
            select(Brand.brand_code, Brand.brand_name).where(Brand.brand_code.in_(unmatched))
        ).all()
    }

    all_labels = ri_session.execute(
        select(source_brand_labels.c.label, source_brand_labels.c.manufacturer_id, core_manufacturers.c.canonical_name)
        .select_from(source_brand_labels)
        .join(core_manufacturers, core_manufacturers.c.id == source_brand_labels.c.manufacturer_id)
    ).all()

    hints: list[PossibleMatch] = []
    for brand_code, brand_name in brand_names.items():
        if not brand_name:
            continue
        needle = brand_name.strip().upper()
        for label, manufacturer_id, manufacturer_name in all_labels:
            if not label:
                continue
            haystack = label.strip().upper()
            if haystack == needle:
                continue  # exact matches are compute_brand_match's job, not a "hint"
            if needle in haystack or haystack in needle:
                hints.append(PossibleMatch(brand_code, manufacturer_id, manufacturer_name, label))

    return hints


def _count_master_products(ri_session: Session, master_products: Table, manufacturer_id: int) -> int:
    return ri_session.execute(
        select(func.count()).select_from(master_products).where(
            master_products.c.brand_id == manufacturer_id
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
