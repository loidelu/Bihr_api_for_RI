"""Brand dictionary derivation from products catalog.

Scans the products table for distinct (brand_code, brand_name) pairs and
upserts them into the brands table. Detects renames by comparing the stored
brand_name against the newly observed one for each brand_code.

Transactional contract: the entire operation happens within a single session
and is only flushed/committed once at the very end. Any exception propagates
to the caller with the session left uncommitted.
"""

from sqlalchemy.orm import Session

from bihr_connector.models import Brand, Product


def sync_brand_dictionary(session: Session) -> dict:
    """Scan distinct (brand_code, brand_name) pairs from products table
    and upsert into brands table.

    Returns:
        dict: {"new": int, "renamed": int, "unchanged": int}
    """
    # Query distinct (brand_code, brand_name) pairs from products where is_present=True
    query = session.query(
        Product.brand_code,
        Product.brand_name,
    ).filter(
        Product.is_present == True,
        Product.brand_code.isnot(None),
        Product.brand_name.isnot(None),
    ).distinct()

    observed_brands = {row[0]: row[1] for row in query}

    new_count = 0
    renamed_count = 0
    unchanged_count = 0

    for brand_code, brand_name in observed_brands.items():
        existing_brand = session.get(Brand, brand_code)

        if existing_brand is None:
            # New brand: create it
            brand = Brand(
                brand_code=brand_code,
                brand_name=brand_name,
                is_active=False,
            )
            session.add(brand)
            new_count += 1
        elif existing_brand.brand_name != brand_name:
            # Rename: update in-place, storing the old name
            existing_brand.previous_brand_name = existing_brand.brand_name
            existing_brand.brand_name = brand_name
            renamed_count += 1
        else:
            # Unchanged
            unchanged_count += 1

    session.flush()

    return {
        "new": new_count,
        "renamed": renamed_count,
        "unchanged": unchanged_count,
    }
