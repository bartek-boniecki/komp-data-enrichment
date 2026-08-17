# HubSpot deal field mapping fixes (Opis / Typ / location)

**Date:** 2026-07-30  
**Status:** Approved for implementation

## Goals

For deals like SkySnap lead_id=164 (Amex / Magazyn Jaćmierz):

1. **Opis transakcji** (`description`) = Kompass project description only.
2. **Agent analysis** = HubSpot timeline **Note** on the deal (not Opis).
3. **Typ inwestycji** = Kompass Typ (`Publiczna` → `publiczne`), not company-name heuristics.
4. Fill HubSpot fields already present in portal: `sektor_podsektor`, `wspol_miasto_budynku`, `wspol_wojewodztwo`, `wspol_ulica_budynku`, `wspol_numer_budynku`.

## Design

- Deterministic parser of Kompass project page text for Typ / Sektor / location labels.
- Merge into `EnrichmentResult` (new optional fields); Claude prompt also asked to fill them when possible.
- `build_deal_properties`: `description` ← `project_description`; analysis body used for Note API + company `komentarz_wewnetrzny`.
- `investment_type_label`: prefer scraped Typ; heuristics as fallback only.
- `HubSpotClient.create_note` + associate to deal; create Note when analysis hash changes (store hash on `lead_exports`).
- New `HUBSPOT_PROP_*` env vars for the location/sector properties.

## Out of scope

- Deleting/archiving old deals or companies.
- Changing Google Sheet columns.
