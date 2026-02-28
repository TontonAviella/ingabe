# Mundi.ai — CoWork Project Brief

Upload this file to Claude CoWork as project context. It gives Claude everything it needs to create marketing materials in Canva, Figma, and other connected tools.

---

## Product Identity

**Name**: Mundi.ai
**Tagline**: "Talk to your map."
**Category**: AI-Native Web GIS Platform
**URL**: mundi.ai
**Built by**: Ingabe (credited to Roger)

## Brand Positioning

Mundi.ai replaces traditional GIS software (QGIS, ArcGIS) with a conversational AI interface. Users upload geographic data and ask questions in plain English. The AI handles geoprocessing, satellite analysis, and data visualisation automatically.

**One-liner**: "The AI-native GIS that lets you talk to your map."

**Elevator pitch**: Mundi.ai is a web-based geographic information system powered by AI. Upload any geodata — shapefiles, satellite imagery, LiDAR, CSVs — and analyse it using natural language. The AI picks the right tools, runs satellite analysis, and shows results live on the map. No desktop software, no coding, no GIS training required.

## Target Audiences

1. **Agricultural analysts & agronomists** — crop monitoring, yield prediction, land use analysis
2. **Government agencies** — district-level planning, land cover mapping, environmental monitoring
3. **NGOs & development organisations** — food security, climate adaptation, rural development
4. **GIS professionals** — faster workflows, no menu clicking, instant satellite data
5. **Data engineers** — PostGIS integration, natural language SQL, spatial ETL

## Visual Identity

**Primary colours** (use in Canva/Figma designs):
- Brand green: `#22C55E` (Tailwind green-500) — represents agriculture, satellite, growth
- Dark background: `#0F172A` (Slate-900) — for map-centric dark UI
- White text: `#F8FAFC` (Slate-50)
- Accent blue: `#3B82F6` (Blue-500) — for water, links
- Alert red: `#EF4444` (Red-500) — for anomalies, built areas

**Typography**:
- Headings: Inter or system sans-serif, bold
- Body: Inter, regular
- Code/data: JetBrains Mono or monospace

**Visual style**:
- Map-centric — always show the map as the hero
- Dark mode UI with colourful data overlays
- Satellite imagery as background texture
- Clean, minimal interface — the data is the star

## Core Features (for content creation)

### 1. AI Chat Interface
Users type questions in natural language. The AI understands map context and invokes the right tools automatically.
- "Show me cropland in Gasabo district"
- "What's the NDVI trend this month?"
- "Buffer these farms by 2 kilometres"
- "Calculate average elevation per district"

### 2. Land Cover Mapping (10m resolution)
ESRI 10m Annual LULC 2024 — satellite-derived land classification.
- 9 classes: Water, Trees, Flooded Vegetation, Crops, Built Area, Bare Ground, Snow/Ice, Clouds, Rangeland
- Two modes: full classification (all colours) and cropland highlight (green crops, grey everything else)
- Pixel-level statistics: exact hectare breakdowns per class
- Clips to any administrative boundary (district/sector/cell/village)

### 3. Satellite Crop Monitoring
Live Sentinel-2 imagery (10m resolution, updated every 5 days).
- NDVI — vegetation health (0-1 scale)
- 6 agricultural indices in one call (NDVI, EVI, NDWI, SAVI, NDRE, NDBI)
- District, sector, cell, and individual field level
- Anomaly alerts for crop stress (z-score analysis)
- Yield risk prediction (Mann-Kendall trend analysis)
- Drought detection (Vegetation Condition Index)
- Crop growth stage identification (phenology analysis)

### 4. Environmental Data
- **Soil**: iSDAsoil 30m — pH, nitrogen, phosphorus, potassium, organic carbon, texture (20+ properties)
- **Weather**: Copernicus AgERA5 — daily temperature, precipitation, solar radiation
- **Emissions**: EDGAR v8.0 — agricultural greenhouse gases (CH4, N2O, CO2, NH3)

### 5. Geoprocessing (40+ tools)
All invoked by natural language:
- Buffer, clip, dissolve, merge, reproject
- Spatial joins, intersections, zonal statistics
- Field calculator, statistics by categories
- Grid generation (hexagons, rectangles)
- Geometry repair

### 6. Data Upload (20+ formats)
- Vector: GeoJSON, Shapefile, FlatGeoBuf, GeoPackage, KML/KMZ
- Raster: GeoTIFF, COG
- Point cloud: LAS/LAZ (3D visualisation)
- Tabular: CSV with lat/lon (auto-geocoded), Google Sheets
- Remote: WFS, ESRI Feature Service, PostGIS, HTTP URLs

### 7. Database Integration
- Connect external PostgreSQL/PostGIS databases
- Natural language SQL generation
- Live layer creation from queries
- Schema discovery and documentation

### 8. Sharing & Embedding
- Interactive map sharing (no GIS software needed to view)
- Embeddable maps for websites
- Social preview images (OG meta)
- Team collaboration with role-based access

## Key Statistics (for marketing copy)

- 10-metre satellite resolution (individual fields visible)
- 9 land cover classes covering all of Africa
- 6 vegetation indices per analysis
- 30m soil data with 20+ properties
- 2,148 cell-level monitoring units in Rwanda
- 40+ geoprocessing tools
- 20+ data format support
- 5-day satellite revisit from Sentinel-2
- Daily weather data from Copernicus

## Content Templates to Create in Canva

1. **Product announcement video** (60s) — use Script 1 from product-scripts.md
2. **Feature walkthrough: Land Cover** (2-3 min) — use Script 2
3. **Feature walkthrough: Crop Monitoring** (2-3 min) — use Script 3
4. **Feature walkthrough: AI Geoprocessing** (2 min) — use Script 4
5. **Social media carousel** — data upload capabilities (Script 6)
6. **Investor/partner presentation** — full product overview (Script 7)
7. **LinkedIn post graphics** — key statistics + screenshots
8. **Twitter/X thread visuals** — one graphic per tweet in the thread

## Figma Assets Needed

1. **Product screenshots** — map view with land cover overlay, chat panel visible
2. **Feature comparison table** — Mundi.ai vs QGIS vs ArcGIS vs Earth Engine
3. **Data flow diagram** — upload → AI → satellite → map → insights
4. **Icon set** — land cover classes (water, trees, crops, built area, etc.)
5. **Colour legend** — matching the 9-class land cover palette
6. **UI mockups** — for presentation decks and website

## Tone of Voice

- **Confident but not arrogant** — "this works" not "we're the best"
- **Technical but accessible** — use GIS terms but explain them
- **Direct** — short sentences, active voice, no filler
- **Show don't tell** — screenshots, numbers, examples over claims
- **Inclusive** — "no GIS expertise needed" is a core message
