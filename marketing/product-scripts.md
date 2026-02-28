# Mundi.ai — Product Content Scripts

Scripts for video walkthroughs, social media, and tutorials.
Designed for use in Canva (video/presentations), Figma (visual assets), and social channels.

---

## Script 1: Product Announcement (60-90 second video)

**Title**: "Meet Mundi.ai — The AI-Native GIS"
**Format**: Canva video template / social media reel
**Audience**: GIS professionals, agricultural analysts, development organisations

---

**[HOOK — 0:00-0:05]**

> "What if you could talk to your map?"

**[PROBLEM — 0:05-0:15]**

> Traditional GIS tools make you click through menus, write queries, and wrestle with data formats.
> You spend more time on the software than on the actual analysis.

**[SOLUTION — 0:15-0:35]**

> Mundi.ai is an AI-native web GIS. Upload your data — shapefiles, GeoJSON, CSVs, GeoTIFFs, even LiDAR point clouds — and just ask questions in plain English.
>
> "Show me cropland in Gasabo district."
> "What's the NDVI trend this month?"
> "Buffer these farms by 2 kilometres and clip to the district boundary."
>
> The AI understands your map, picks the right geoprocessing tools, and shows you the result — live on the map.

**[FEATURES — 0:35-0:60]**

> - 10-metre resolution land cover from satellite imagery
> - Real-time crop health monitoring with Sentinel-2
> - Soil analysis, weather data, and yield risk predictions
> - Connect your own PostGIS database — query it with natural language
> - Share interactive maps with anyone — no GIS software needed

**[CTA — 0:60-0:75]**

> Stop clicking through menus. Start talking to your map.
> Try Mundi.ai free at mundi.ai

---

## Script 2: Feature Deep-Dive — Land Cover Analysis (2-3 minute tutorial)

**Title**: "See Every Field at 10-Metre Resolution"
**Format**: Screen recording walkthrough with voiceover
**Audience**: Agricultural analysts, NGOs, government agencies

---

**[INTRO — 0:00-0:15]**

> In this walkthrough, I'll show you how Mundi.ai uses ESRI's 10-metre satellite classification to map land cover — and how you can get cropland statistics for any area in seconds.

**[STEP 1: ADD LAND COVER — 0:15-0:45]**

> Open the chat and type: "Show me the land cover for Kigali"
>
> *(Show screen: AI processes the request, adds the ESRI 10m LULC 2024 layer)*
>
> The AI automatically detects you're asking about land cover. It adds a coloured overlay showing 9 land cover classes:
> - Green for trees
> - Pink for cropland
> - Red for built-up areas
> - Blue for water
> - Orange for rangeland
>
> This is real satellite data at 10-metre resolution — you can see individual fields.

**[STEP 2: CROPLAND MODE — 0:45-1:15]**

> Now type: "Highlight just the cropland"
>
> *(Show screen: map switches to cropland mode — green highlights, grey background)*
>
> The cropland mode highlights agricultural areas in green and mutes everything else. This is useful when you want to focus on where farming is happening.

**[STEP 3: GET STATISTICS — 1:15-1:45]**

> Ask: "How much cropland is in this area?"
>
> *(Show screen: AI returns land cover statistics with hectare breakdowns)*
>
> The AI reads the actual satellite pixels and gives you a breakdown:
> - Crops: 7,608 hectares (27%)
> - Rangeland: 11,554 hectares (41%)
> - Built Area: 6,298 hectares (23%)
> - Trees: 2,107 hectares (8%)
>
> These numbers come from counting every 10m x 10m pixel — no estimates.

**[STEP 4: CLIP TO BOUNDARIES — 1:45-2:15]**

> You can scope this to any administrative boundary.
> Type: "Show cropland for Bugesera district only"
>
> *(Show screen: land cover clipped to district boundary)*
>
> The AI clips the satellite layer to the exact district boundary from our PostGIS database.
> Works at district, sector, cell, or even village level.

**[WRAP-UP — 2:15-2:30]**

> That's satellite-based land cover analysis — no downloads, no desktop software, no coding.
> Just ask your map.

---

## Script 3: Feature Deep-Dive — Crop Health Monitoring (2-3 minute tutorial)

**Title**: "Monitor Crop Health from Space — In Real Time"
**Format**: Screen recording walkthrough
**Audience**: Agronomists, farm managers, extension workers

---

**[INTRO — 0:00-0:10]**

> Mundi.ai connects directly to Sentinel-2 satellite imagery to monitor vegetation health — updated every 5 days.

**[STEP 1: NDVI OVERVIEW — 0:10-0:40]**

> Type: "What's the vegetation health in Musanze district?"
>
> *(Show screen: AI returns NDVI statistics — mean, min, max, trend)*
>
> NDVI is the standard measure of vegetation health. Values range from 0 (bare soil) to 1 (dense vegetation).
> The AI returns weekly statistics: mean NDVI, standard deviation, and valid pixel count — all computed from the latest Sentinel-2 pass.

**[STEP 2: MULTI-INDEX — 0:40-1:15]**

> Ask: "Run a full agricultural index analysis for Gasabo"
>
> *(Show screen: 6 indices returned — NDVI, EVI, NDWI, SAVI, NDRE, NDBI)*
>
> You get 6 indices in one call:
> - NDVI — overall vegetation health
> - EVI — enhanced vegetation index (corrects for atmospheric effects)
> - NDWI — water content in plants
> - SAVI — soil-adjusted vegetation (useful for sparse canopy)
> - NDRE — nitrogen and chlorophyll content
> - NDBI — built-up area detection
>
> All computed on-the-fly from the latest satellite imagery.

**[STEP 3: INDIVIDUAL FIELDS — 1:15-1:45]**

> Upload your own field boundaries — shapefile, GeoJSON, or even a CSV with coordinates.
> Then ask: "What's the crop health for my fields?"
>
> *(Show screen: per-parcel NDVI results)*
>
> Each parcel gets its own statistics at 10-metre native Sentinel-2 resolution.

**[STEP 4: ALERTS — 1:45-2:15]**

> Ask: "Are there any crop stress alerts?"
>
> *(Show screen: anomaly alerts with severity levels)*
>
> The system runs z-score analysis on NDVI time series to detect where vegetation has dropped significantly below normal. You get severity levels — moderate or high — with affected locations.

**[CTA — 2:15-2:30]**

> Satellite crop monitoring — no remote sensing expertise required.

---

## Script 4: Feature Deep-Dive — AI Geoprocessing (2 minute tutorial)

**Title**: "GIS Analysis in Plain English"
**Format**: Screen recording
**Audience**: GIS users tired of menu-driven workflows

---

**[INTRO — 0:00-0:10]**

> Every GIS operation you normally do through menus — buffers, clips, spatial joins, zonal statistics — you can do by just typing what you want.

**[DEMO 1: BUFFER + CLIP — 0:10-0:40]**

> Upload a points layer — say, weather stations.
> Type: "Buffer these stations by 5 kilometres and clip to the district boundary"
>
> *(Show screen: AI calls native_buffer → qgis_clip → result appears on map)*
>
> The AI picks the right tools, chains them together, and shows you the result.
> Behind the scenes it's running QGIS processing algorithms — but you never touch a menu.

**[DEMO 2: SPATIAL JOIN — 0:40-1:10]**

> "Join the soil data to the district polygons by location"
>
> *(Show screen: AI calls native_joinattributesbylocation → attribute table shows joined columns)*
>
> Spatial joins that used to take 5 clicks and a settings dialog — done in one sentence.

**[DEMO 3: ZONAL STATISTICS — 1:10-1:40]**

> "Calculate average elevation for each district from the DEM"
>
> *(Show screen: AI calls native_zonalstatisticsfb → choropleth map with results)*
>
> Zonal statistics — summarising raster values within polygons — is one of the most powerful GIS operations. Here it's just a sentence.

**[CTA — 1:40-2:00]**

> 40+ geoprocessing tools. Zero menus. Just describe what you need.

---

## Script 5: Feature Deep-Dive — PostGIS Connection (90 seconds)

**Title**: "Connect Your Database. Query It in English."
**Format**: Screen recording
**Audience**: Data engineers, GIS database administrators

---

**[INTRO — 0:00-0:10]**

> Already have spatial data in PostgreSQL? Connect it directly to Mundi.ai and query it with natural language.

**[DEMO — 0:10-0:50]**

> Click "Add Data Source" → PostGIS → enter your connection string.
>
> *(Show screen: connection established, tables discovered)*
>
> Mundi.ai discovers your tables, inspects the schemas, and detects geometry columns automatically.
>
> Now type: "Show me all parcels larger than 5 hectares"
>
> *(Show screen: AI generates SQL → executes → polygons appear on map)*
>
> The AI writes the SQL, validates it, runs it against your database, and renders the result as a map layer.
> You can inspect the query, modify it, or save the result as a new layer.

**[SHARING — 0:50-1:10]**

> Share the map with colleagues — they see the live data without needing database access.
> Embed it in your website or dashboard with one click.

**[CTA — 1:10-1:20]**

> Your database. Your map. Your language.

---

## Script 6: Overview — Data Upload Capabilities (60 seconds)

**Title**: "Bring Any Geodata"
**Format**: Social media short / carousel
**Audience**: All GIS users

---

**[VISUAL CAROUSEL — each slide 5-8 seconds]**

**Slide 1: Vector Data**
> GeoJSON, Shapefile (ZIP), FlatGeoBuf, GeoPackage, KML/KMZ
> Upload → instantly on your map

**Slide 2: Raster Data**
> GeoTIFF, Cloud-Optimized GeoTIFF (COG)
> Automatic reprojection, band statistics

**Slide 3: Point Clouds**
> LAS / LAZ LiDAR files
> 3D visualisation with deck.gl

**Slide 4: Tabular Data**
> CSV with lat/lon columns → auto-geocoded
> Google Sheets links → live data

**Slide 5: Remote Services**
> WFS endpoints, ESRI Feature Services
> PostGIS databases, HTTP URLs

**Slide 6: CTA**
> 20+ formats. Drag and drop. Done.

---

## Script 7: Full Product Overview — Investor / Partner Deck (3-5 minutes)

**Title**: "Mundi.ai — AI-Native GIS for Agriculture and Development"
**Format**: Canva presentation / pitch deck voiceover
**Audience**: Investors, development partners, government agencies

---

**[SLIDE 1: THE PROBLEM]**

> 80% of agricultural decisions in sub-Saharan Africa are made without satellite data — not because the data doesn't exist, but because the tools are too complex.
>
> QGIS takes months to learn. ArcGIS costs thousands per licence. Google Earth Engine requires Python.

**[SLIDE 2: THE SOLUTION]**

> Mundi.ai is an AI-native web GIS. Users type what they want in plain English, and the AI handles the rest — geoprocessing, satellite analysis, data visualisation, database queries.
>
> No desktop software. No coding. No GIS training.

**[SLIDE 3: THE PLATFORM]**

> - Upload any geodata format (shapefiles, GeoJSON, GeoTIFF, LiDAR, CSV)
> - 40+ geoprocessing tools invoked by natural language
> - Live satellite imagery from Sentinel-2 (10m resolution, updated every 5 days)
> - ESRI 10m land cover classification (9 classes, continent-wide)
> - iSDAsoil 30m soil properties (pH, nutrients, texture)
> - Copernicus weather data (temperature, precipitation, solar radiation)
> - EDGAR emissions data (agricultural greenhouse gases)
> - PostGIS database connections with natural language SQL

**[SLIDE 4: AGRICULTURAL INTELLIGENCE]**

> Pre-built analytics for Rwanda (expanding to East Africa):
> - Crop health monitoring at district, sector, cell, and individual field level
> - Yield risk prediction using Mann-Kendall trend analysis
> - Drought detection via Vegetation Condition Index
> - Crop growth stage identification from NDVI phenology
> - Anomaly alerts for crop stress and disease
> - Emissions tracking per district

**[SLIDE 5: TECHNOLOGY]**

> - FastAPI backend with async processing
> - MapLibre GL + deck.gl for web-native 3D mapping
> - Apache Iceberg lakehouse for versioned vector data
> - Dagster pipelines for nightly satellite data processing
> - OpenAI function calling for AI tool dispatch
> - QGIS Processing server for enterprise geoprocessing
> - Cloud-native: Docker, S3, PostgreSQL, Redis

**[SLIDE 6: TRACTION]**

> *(Insert metrics: active users, maps created, satellite analyses run, data processed)*

**[SLIDE 7: CTA]**

> The future of GIS is conversational.
> mundi.ai

---

## Social Media Copy

### LinkedIn Post — Product Launch

> We built Mundi.ai because we believe GIS should be as easy as asking a question.
>
> Upload your data. Ask: "Show me cropland in this district." The AI reads satellite imagery at 10-metre resolution and colours your map — live.
>
> 40+ geoprocessing tools. Real-time Sentinel-2 crop monitoring. Soil analysis. Weather data. Yield predictions. All through natural language.
>
> No QGIS. No ArcGIS licence. No Python scripts.
>
> Try it at mundi.ai
>
> #GIS #AI #Agriculture #RemoteSensing #SpatialData

---

### Twitter/X Thread — Feature Highlights

> **1/6** We just shipped 10-metre land cover for all of Africa.
>
> Ask Mundi.ai: "Show me the cropland in Kigali" — and you get pixel-level accuracy from ESRI's 2024 satellite classification. Every field. Every building. Every tree.
>
> **2/6** Nine land cover classes: water, trees, flooded vegetation, crops, built area, bare ground, snow/ice, clouds, and rangeland.
>
> You can toggle between full classification view and cropland-only mode.
>
> **3/6** But it's not just pretty tiles.
>
> Ask "How much cropland is in Bugesera district?" and the AI counts actual 10m pixels and returns hectare breakdowns. No estimates — real measurements.
>
> **4/6** Clip to any administrative boundary: district, sector, cell, or village. Or draw a custom area. Or use coordinates from another layer.
>
> The AI figures out the right boundary automatically.
>
> **5/6** Combine with our other datasets:
> - Sentinel-2 NDVI for crop health trends
> - iSDAsoil for nutrient analysis
> - AgERA5 weather for growing conditions
> - EDGAR for agricultural emissions
>
> All queryable in plain English.
>
> **6/6** This is what GIS should be: ask a question, get an answer on a map.
>
> No menus. No projections dialog. No export-to-CSV-import-to-Excel.
>
> Try it: mundi.ai

---

### Instagram / Short-form Caption

> GIS without the GIS degree.
>
> Upload your data. Ask your question. See it on the map.
>
> 10m satellite imagery. Real-time crop health. AI geoprocessing.
>
> mundi.ai — talk to your map.

---

## Messaging Framework (for all content)

### Tagline Options
1. "Talk to your map."
2. "GIS, meet AI."
3. "Ask your map anything."
4. "The AI-native GIS."

### Key Messages (use 2-3 per piece)
1. **No GIS expertise needed** — natural language replaces menus and dialogs
2. **Real satellite data** — 10m resolution, updated every 5 days, not estimates
3. **Instant analysis** — geoprocessing, statistics, and visualisation in seconds
4. **Any data format** — 20+ formats supported, drag and drop
5. **Agricultural intelligence** — crop health, yield risk, drought, soil, weather
6. **Your database, your language** — connect PostGIS databases and query with English

### Proof Points
- 9-class land cover at 10-metre resolution (ESRI 2024)
- 6 vegetation indices computed on-the-fly (NDVI, EVI, NDWI, SAVI, NDRE, NDBI)
- 30m soil properties from iSDAsoil (20+ metrics per location)
- 40+ geoprocessing tools (buffer, clip, dissolve, spatial join, zonal statistics)
- 2,148 cell-level monitoring units across Rwanda
- Daily weather data from Copernicus AgERA5

### Differentiators vs. Competitors
| vs. QGIS | vs. ArcGIS Online | vs. Google Earth Engine |
|-----------|-------------------|------------------------|
| No desktop install | No per-user licence | No Python required |
| AI-driven workflow | AI-driven workflow | Web-native UI |
| Real-time satellite | Real-time satellite | Easier data upload |
| Natural language | Natural language | Natural language |
| Free to start | Free to start | Immediate results |
