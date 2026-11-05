# BIHR V3 GraphQL probe (2026-11-05)

Exploratory probe only — no ingestion code was built from this. Run against the real, production BIHR account already used for this kind of API access (credentials from this repo's `.env`, never printed or committed).

## What was tried

1. Authenticated via `POST /api/v3/Authentication/Token` with `UserName`/`PassWord` as `application/x-www-form-urlencoded` form data (not multipart — see `bihr_client.py` and the reasoning below). Result: `200 OK`, body keys `access_token`, `token_type`, `expires_in`.
2. Probed `POST /api/v3/Products/GraphQL` (the endpoint used by the proven reference client's `V3Client.graphql()` in `ebihr_v3_probe.py`) with `{"query": "{ __typename }"}`, bearer token from step 1.
3. Ran GraphQL schema introspection (`__schema`, `__type`) against the same endpoint to enumerate what is queryable, without materializing any real product data.

## Raw responses (redacted of the bearer token; nothing else sensitive in a catalog/pricing schema)

**Step 2 — minimal query**

Request: `POST {base_url}/api/v3/Products/GraphQL` with `{"query": "{ __typename }"}`

```json
HTTP 200, Content-Type: text/plain; charset=utf-8
{"data":{"__typename":"Query"}}
```

**Step 3 — `__schema` introspection (top-level types)**

```json
HTTP 200
{"data":{"__schema":{"queryType":{"name":"Query"},"types":[
  {"name":"__Schema","kind":"OBJECT"},{"name":"String","kind":"SCALAR"},
  {"name":"__Type","kind":"OBJECT"},{"name":"__TypeKind","kind":"ENUM"},
  {"name":"__Field","kind":"OBJECT"},{"name":"__InputValue","kind":"OBJECT"},
  {"name":"Boolean","kind":"SCALAR"},{"name":"__EnumValue","kind":"OBJECT"},
  {"name":"__Directive","kind":"OBJECT"},{"name":"__DirectiveLocation","kind":"ENUM"},
  {"name":"Query","kind":"OBJECT"},{"name":"Product","kind":"OBJECT"},
  {"name":"Details","kind":"OBJECT"},{"name":"Attribute","kind":"OBJECT"},
  {"name":"BillOfMaterial","kind":"OBJECT"},{"name":"Int","kind":"SCALAR"},
  {"name":"Document","kind":"OBJECT"},{"name":"Generic","kind":"OBJECT"},
  {"name":"Variation","kind":"OBJECT"},{"name":"GTINs","kind":"OBJECT"},
  {"name":"GtinType","kind":"ENUM"},{"name":"Picture","kind":"OBJECT"},
  {"name":"DateTime","kind":"SCALAR"},{"name":"SaleCategory","kind":"OBJECT"},
  {"name":"Universe","kind":"OBJECT"},{"name":"Price","kind":"OBJECT"},
  {"name":"Decimal","kind":"SCALAR"},{"name":"ScaledDiscount","kind":"OBJECT"},
  {"name":"Stock","kind":"OBJECT"},{"name":"ProductsCriteria","kind":"INPUT_OBJECT"}
]}}}
```

**`Query` type's only field**

```json
{"data":{"__type":{"fields":[{
  "name":"products",
  "args":[
    {"name":"since","type":{"name":"String","kind":"SCALAR"}},
    {"name":"pageNumber","type":{"name":"Int","kind":"SCALAR"}},
    {"name":"resultSize","type":{"name":"Int","kind":"SCALAR"}},
    {"name":"language","type":{"name":"String","kind":"SCALAR"}},
    {"name":"productsCriteria","type":{"name":"ProductsCriteria","kind":"INPUT_OBJECT"}}
  ],
  "type":{"name":null,"kind":"LIST","ofType":{"name":"Product","kind":"OBJECT"}}
}]}}}
```

**`Product` type's fields** — just a composite wrapper:

```json
{"data":{"__type":{"fields":[
  {"name":"details","type":{"name":"Details","kind":"OBJECT"}},
  {"name":"price","type":{"name":"Price","kind":"OBJECT"}},
  {"name":"stock","type":{"name":"Stock","kind":"OBJECT"}}
]}}}
```

**`Details` type's fields** (the interesting one):

```
attributes: [Attribute]
barCode: String
billOfMaterials: [BillOfMaterial]
brandCode / brandImageUrl / brandName: String!
commodityCode: String
countryOfOrigin: String
deliveryPlant: String
documents: [Document]
familyCode / familyName: String!
followupPartNumber: String
genericProduct: Generic
groupCode / groupName: String!
gtinCodes: [GTINs]          # plural, typed (see GtinType enum) — richer than a single BarCode flag
hazMatCode: String
height / width / length / volume / weight: Int!
htmlDescription: String
isDiscontinued / isDropShippingAllowed / isOnDemand: Boolean!
legacyPartNumber: String
name: String
partNumber: String!
pictures: [Picture]
predecessorPartNumber: String
publicationDate: DateTime
salesCategories: [SaleCategory]
segmentCode / segmentName: String!
supplierPartNumber: String
title: String
updateDateTime: DateTime
vehicleUniverses: [Universe]   # fitment-adjacent data
volume / weight / width: Int!
```

**Rate limit observed:** a follow-up query attempted 1 second after the prior one returned:

```json
HTTP 429
{ "message": "API calls quota exceeded!", "details": "Maximum allowed: 1 per 1s. Please try again in 1 second(s)." }
```

So GraphQL enforces its own throttle (1 request/second), separate from whatever applies to the REST endpoints.

## One-sentence verdict

GraphQL's `products` query is a one-stop composite of the same three REST building blocks (`Details`/`Price`/`Stock`, matching `Products/Details`, `Products/Prices`, `Products/Stocks`) but its `Details` sub-object is meaningfully richer than what the bulk `Catalogs/Request` → `Products` ZIP exposes — `billOfMaterials`, `documents`, `pictures`, `attributes`, `genericProduct`, `vehicleUniverses` (fitment-adjacent), and multiple typed `gtinCodes` (vs. the bulk catalog's single flat barcode-like field) are visible here and not in the bulk Products catalog fields this plan's Task 3 normalizes — worth a future task if fitments/media/BOM ever come into scope, but explicitly out of scope for this phase per the plan.

## Step 1 verification: auth/session complexity

Read `ebihr_v3_catalogs.py` and `ebihr_v3_probe.py` (`bihr_catalog_downloader_v6_6_7`) end to end, plus the shorter `bihr_prices_v3.py`'s `BihrClient` (`bihr_prices_v3_v1.0.5`). Confirmed:

- No CSRF token anywhere in the auth or catalog flow.
- No 2FA / MFA step.
- No session cookie dependency — every authenticated call is a stateless `Authorization: Bearer <token>` header; the `requests.Session` object in the reference clients is reused only for connection pooling/retries, not for cookie-based session state.
- Token TTL is `expires_in` (~1799s) from the token response; the reference clients track `acquired_monotonic` and treat 401 as "reauthenticate once and retry," which is sufficient — no separate session/TTL concept beyond the bearer token's own `expires_in`.
- The real `/api/v3/Authentication/Token` call it this probe made returned `200` using `application/x-www-form-urlencoded` body (`UserName`/`PassWord`), matching `bihr_prices_v3.py`'s documented finding that BIHR's own OpenAPI spec claims `multipart/form-data` but production actually requires form-urlencoded (multipart causes HTTP 500 "Unrecognized Guid format" — not re-tested here to avoid burning quota on a call already known to fail, per the existing reference comment in `bihr_prices_v3.py` lines 224-226).

Conclusion: no hidden complexity beyond the documented Bearer token flow. Safe to proceed with the simple auth/retry-once model specified in the plan.
