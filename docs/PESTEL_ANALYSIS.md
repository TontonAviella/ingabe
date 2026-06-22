# Ingabe/Mundi PESTEL Analysis

Purpose: keep the geospatial intelligence platform honest against the real
world around it. PESTEL is not a product benchmark. It is the external-risk
layer that sits beside technical tests, field validation, customer discovery,
and financial metrics.

Last reviewed: 2026-06-18

## Method

PESTEL checks six external forces: Political, Economic, Social,
Technological, Environmental, and Legal. For each item, score:

| Score | Likelihood | Impact |
|---|---|---|
| 1 | Unlikely | Low effect |
| 2 | Possible | Noticeable effect |
| 3 | Likely | Material effect |
| 4 | Very likely | Serious effect |
| 5 | Already happening | Critical effect |

Priority score = likelihood x impact.

## Source Anchors

- Oxford College of Marketing describes PESTEL as a macro-environment scan and
  recommends scoring items by likelihood and impact before feeding the result
  into SWOT.
- Rwanda's PSTA5 agriculture plan runs for 2024-2029 and is framed around food
  systems and climate resilience. MINAGRI names land scarcity, low productivity,
  post-harvest losses, climate shocks, limited finance access, and low market
  penetration as key challenges.
- Rwanda's agriculture sector remains economically central. The World Bank's
  April 2025 Rwanda Economic Update reports agriculture employing about 43% of
  the workforce and contributing about 27% of GDP.
- Rwanda Civil Aviation Authority requires UAS/drone registration and formal
  operator permitting. Visit Rwanda notes permits are required for recreational
  and commercial drone use.
- Rwanda Law No. 058/2021 on protection of personal data and privacy was
  gazetted on 2021-10-15.
- Rwanda climate/adaptation materials and PSTA5 make climate resilience a
  central planning issue for agriculture and food systems.

## Priority Scorecard

| Area | External factor | Why it matters | Likelihood | Impact | Priority | Metric to track | System response |
|---|---|---|---:|---:|---:|---|---|
| Political | Agriculture modernization is a national priority through PSTA5 and NST2 | Aligns Ingabe with public-sector priorities and partner funding | 5 | 5 | 25 | Number of government/NGO/agri partner workflows tested | Package Sage reports around PSTA5 language: productivity, resilience, finance, market access |
| Political | Drone work depends on aviation approval and local operating permissions | Slow permits can block data collection even when the platform works | 4 | 5 | 20 | Permit lead time, failed/blocked drone jobs | Support non-drone satellite/rain/admin analysis when flights are delayed |
| Economic | Smallholder market has high need but limited direct ability to pay | Product may be valuable but not paid for by individual farmers | 5 | 5 | 25 | Willingness-to-pay by segment, CAC, cost per hectare analyzed | Prioritize B2B/B2G buyers: cooperatives, insurers, NGOs, districts, agribusinesses |
| Economic | Agriculture is large but productivity constrained | Strong business case if we reduce losses or technician time | 5 | 4 | 20 | Hectares analyzed per technician-day, loss avoided, action completion rate | Price around operational value, not only software seats |
| Social | Users should not need to understand H3, GeoParquet, PMTiles, or model internals | Confusing technical language reduces trust and adoption | 5 | 4 | 20 | Sage answer clarity score, retry rate, support questions | Hide implementation terms; show "risk areas", "affected homes", "fields to inspect" |
| Social | Trust requires visible evidence on the map | If Sage says "agriculture" over houses, users lose confidence | 5 | 5 | 25 | False-context rate, evidence-layer coverage, field validation agreement | Require Sage to state evidence basis and confidence; join buildings/roads/farms before firm claims |
| Technological | Fast drone ingest is a product-defining requirement | Large TIFFs cannot make Hermes/Sage wait for batch GIS work | 5 | 5 | 25 | Time to first map, raw tile latency, COG background duration | Raw-first rasterd, background COG, cache outputs, PostHog telemetry |
| Technological | H3 must be zoom-adaptive | Fixed cells become misleading at close zooms | 5 | 4 | 20 | H3 resolution pyramid presence, cell count by zoom, render failures | Use zoom bands: coarse cells out, finer cells in as zoom increases |
| Technological | GeoAI is still incomplete for buildings, roads, crop damage, and infrastructure damage | Visual screening is not the same as detection | 5 | 5 | 25 | Model precision/recall, human validation rate, evidence coverage | Separate "screening" from "confirmed detection"; integrate Open Buildings, Tessera-like embeddings, landcover, road/drainage layers |
| Environmental | Rain, floods, drought, erosion, and drainage are core demand drivers | Environmental events create urgent technician workflows | 5 | 5 | 25 | Forecast-to-impact latency, admin/H3 affected area, alert accuracy | Keep rain-impact, flood, drainage, terrain, and field-action tools in Sage's fast path |
| Environmental | Climate shocks affect agriculture and infrastructure together | Same system must serve agriculture, housing, and environment contexts | 5 | 4 | 20 | Cross-domain questions answered, asset exposure counts | Add intent routing: agriculture vs housing vs infrastructure vs environment |
| Legal | Personal data/privacy law applies to collected and processed data | Drone imagery may capture homes, people, property, or sensitive locations | 4 | 5 | 20 | Data retention coverage, consent records, deletion requests | Minimize personal data, redact where possible, enforce org isolation, audit access |
| Legal | Drone imagery rights and government/admin data licenses matter | Bad data rights can block deployment with serious partners | 4 | 4 | 16 | Dataset license status, source attribution coverage | Track source, license, consent, and allowed use in layer metadata |

## Strong Opinion

The highest-risk mistake is not speed. Speed is necessary, but the larger
danger is confident analysis without enough evidence. If Sage says a residential
area is agriculture because the raster is green, the product loses trust.

So the rule is:

```text
Fast first map, honest first answer, stronger answer as evidence layers attach.
```

That means:

1. Uploads must show a map quickly.
2. Sage must say what evidence it used.
3. H3 is internal. Users see risk areas and inspection priorities.
4. RGB drone imagery is visual screening, not proof of crop damage, roads,
   buildings, or drainage.
5. Firm claims require matching evidence layers or models.

## Product Metrics

| Question | Metric |
|---|---|
| Is the system fast enough? | Time to first map, tile latency, H3 analysis latency, Sage response latency |
| Is the answer trusted? | User accepts answer, user asks fewer retries, field validation agreement |
| Is the map useful? | Layer rendered, zoom-adaptive H3 present, evidence visible, click inspection used |
| Is the business valuable? | Hectares analyzed, technician hours saved, losses avoided, claims/alerts supported |
| Is the system compliant? | Consent/source metadata coverage, data retention policy coverage, audit log coverage |
| Is the model honest? | Evidence-basis present, confidence stated, no unsupported asset/crop claims |

## Operating Rules For Sage/Hermes

Sage should not expose PESTEL, H3, PMTiles, GeoParquet, or raster internals to
normal users unless they ask technically. Sage should translate tool output into
operational language:

- "These areas need field inspection."
- "These homes/buildings may be exposed, but this needs building evidence."
- "This is a visual vegetation screening, not confirmed crop damage."
- "Rain risk is highest in these cells and villages."
- "The map is ready now; higher-quality background processing is still running."

Sage should choose tools by intent:

| User intent | Tool path |
|---|---|
| "How many hectares?" | Raster metadata/hectare tool |
| "Where is risk in this drone map?" | Raster H3 context layer with zoom-adaptive pyramid |
| "Are houses affected?" | Open Buildings or footprint exposure plus raster context |
| "What will rain damage?" | Rain impact plus admin/H3 exposure plus known assets |
| "What should technician do?" | Evidence summary, priority cells, admin area, action checklist |
| "Is this agriculture or housing?" | Landcover/buildings/roads/farms evidence before firm answer |

## Immediate Work Backlog

| Priority | Work | Success metric |
|---:|---|---|
| 1 | Add evidence-gating to Sage answers | Every spatial answer includes evidence basis and confidence |
| 1 | Connect Open Buildings/footprints to raster-H3 exposure | Housing/infrastructure questions stop relying on RGB context alone |
| 1 | Keep PostHog events for raster/H3/Sage tool paths | Every generated map layer has latency, cell count, persistence, and render status |
| 2 | Add field-validation feedback buttons | Technician can mark "correct", "wrong", "needs visit" |
| 2 | Add partner-ready pricing metrics | Cost per hectare, cost per technician workflow, cost per alert |
| 2 | Add legal/source metadata dashboard | Layer source, license, consent, retention, and owner are visible internally |
| 3 | Add quarterly PESTEL review ritual | Top 10 risk/opportunity list updated every quarter |

## Sources

- Oxford College of Marketing: https://blog.oxfordcollegeofmarketing.com/2016/06/30/pestel-analysis/
- MINAGRI PSTA5 launch: https://www.minagri.gov.rw/updates/news-details/rwanda-launches-5th-strategic-plan-for-agriculture-transformation
- FAOLEX PSTA5 PDF: https://faolex.fao.org/docs/pdf/rwa233660.pdf
- World Bank Rwanda Economic Update, April 2025: https://openknowledge.worldbank.org/entities/publication/b59bb50d-4765-43c8-a566-3e09bb22765c
- Rwanda Civil Aviation Authority drones page: https://www.caa.gov.rw/drones
- Visit Rwanda drone rules summary: https://visitrwanda.com/facts/drones/
- Rwanda personal data and privacy law: https://rwandalii.org/akn/rw/act/law/2021/58/eng%402021-10-15
- MINICT announcement on data protection law: https://www.minict.gov.rw/news-detail/rwanda-passes-new-law-protecting-personal-data
- Rwanda NDC 3.0 climate plan: https://unfccc.int/sites/default/files/2025-12/Rwanda%20NDC3.0%20Final.pdf
- NAP Global Network Rwanda agriculture adaptation case study: https://napglobalnetwork.org/wp-content/uploads/2024/03/rwanda-mel-agriculture-sector-case-studies-v3.pdf
