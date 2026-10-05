# Ingabe Training Manual

**A guide for farmers, insurers, agronomists and agricultural scientists in Rwanda**

---

## Table of Contents

1. [What is Ingabe?](#1-what-is-ingabe)
2. [Signing In](#2-signing-in)
3. [Your Maps](#3-your-maps)
4. [The Map Workspace](#4-the-map-workspace)
5. [Adding Data](#5-adding-data)
6. [Working with Layers](#6-working-with-layers)
7. [Sage, Your Assistant](#7-sage-your-assistant)
8. [Drone Images](#8-drone-images)
9. [Vegetation from Satellites](#9-vegetation-from-satellites)
10. [Weather, Drought and Crop Risk](#10-weather-drought-and-crop-risk)
11. [Insurance](#11-insurance)
12. [Soil and Water](#12-soil-and-water)
13. [Radar: Through the Clouds](#13-radar-through-the-clouds)
14. [Land Cover](#14-land-cover)
15. [Rwanda's Districts, Sectors, Cells and Villages](#15-rwandas-districts-sectors-cells-and-villages)
16. [The Knowledge Brain](#16-the-knowledge-brain)
17. [Data Sources and Update Times](#17-data-sources-and-update-times)
18. [Common Workflows](#18-common-workflows)
19. [Troubleshooting](#19-troubleshooting)
20. [Glossary](#20-glossary)

---

## 1. What is Ingabe?

Ingabe is a map you use in your web browser, with an assistant called **Sage** that answers questions about Rwanda's fields, weather and crops. You do not need to know GIS: you add your drone images or field shapes, then ask Sage in plain language.

**What you can do:**

- Upload drone orthophotos and field boundaries and see them on a map
- Ask Sage how green or stressed a field is, where the problem patches are, and how two flights compare
- Get vegetation (NDVI), drought, rainfall, forecast, soil and soil-moisture information for any district, sector, cell, village or field in Rwanda
- Get an insurance view of a place: rainfall against the season, drought indices and trigger status
- Add reports and documents that Sage can read and quote

**Who it is for:**

| User | Typical questions |
|------|------------------|
| **Farmers** | How is my field doing? Will it rain this week? |
| **Insurers** | Is the drought trigger met in this sector? How reliable is the rainfall record? |
| **Agronomists** | Where are the stressed patches? What stage are the crops at? |
| **Scientists** | What do the indices, anomalies and trends say, and from which data? |

---

## 2. Signing In

- Open Ingabe in your browser. If you are not signed in, it sends you to the sign-in page automatically.
- If your session expires while you are on a map, a **Sign in** button appears; click it to continue.
- **Your organization:** the left sidebar shows your organization's name and your role (Owner, Admin or Member). Click it to switch between your organizations or your **Personal workspace**.
- **Sign out:** click your name or picture in the left sidebar, then **Sign out**.

---

## 3. Your Maps

After signing in you see **Your Maps**, a grid of your map projects (12 per page).

| Action | How |
|--------|-----|
| Create a map | Click **New Map** |
| Open a map | Click its card or **Open** |
| Delete a map | Hover over the card and click the red trash icon. There is no confirmation step. |
| See deleted maps | Tick **Show recently deleted**. Deleted maps can be seen but not restored from the app. |

Each card shows a preview of the map, its name and when it was last edited.

---

## 4. The Map Workspace

```
+--------------------------------------------------------------------+
| [Sidebar] | Layer panel   |          Map            |  Chat panel   |
|  Home     | Map title     |                         |  (Sage)       |
|  Recent   | Layers        |  District › Sector ›    |  Previous     |
|  maps     |               |  Cell › Village         |  chats        |
|  Org      | Previous/Zoom/|                         |  Map history  |
|  Account  | Next   [+]    |  [Message to Sage]      |  New Chat     |
+--------------------------------------------------------------------+
```

### Left sidebar
**Home**, your three most recently edited maps, your organization, and your account menu. Collapse or expand it with the arrow.

### Layer panel
- **Map title:** click to rename; it saves after a second.
- **Connection light:** green when the map is connected to Sage, red when not.
- **Layers:** every layer on the map (see [Section 6](#6-working-with-layers)).
- **Previous location / Zoom / Next location:** move between the places you have looked at.
- **+ (Add data):** see [Section 5](#5-adding-data).

### The map
- Zoom buttons are at the top right, the scale bar at the bottom left.
- **Choose basemap** (globe icon): Satellite (default), OpenStreetMap, OpenFreeMap, Topographic, Dark Matter or Voyager. Your choice is saved with the map.
- **District, sector, cell and village outlines** are drawn on the map and follow the zoom: districts when zoomed out, then sectors, cells and villages as you zoom in. The ladder at the top (**District › Sector › Cell › Village**) shows the current level. Hover over a unit to see its name, the units it belongs to, and its vegetation index (NDVI) for the latest week. When a level has no values of its own, it shows the value of the level above and says so. Turn the outlines off or on with the map button at the top right.
- **Click a feature** to see its attributes, zoom to it, or deselect it. While a feature is selected, Sage can see it and will use it as the place you mean.
- A **Legend** box appears when a visible layer has one.

### Chat panel (Sage)
- Type your question in the box at the bottom of the map and press Enter. **Cancel** stops Sage's current answer.
- The right-hand panel lists **Previous chats**, the map's history (which layers Sage or you added or removed, and when), and **New Chat**.
- On small screens, open the chat history and the layer panel with the buttons at the edge of the screen.

---

## 5. Adding Data

Click **+** in the layer panel:

### Upload drone or map files
Orthophotos, field shapes or points. You can also drag files onto the map.

| What | File types |
|------|-----------|
| Drone orthophotos and rasters | GeoTIFF (`.tif`, `.tiff`), `.dem` |
| Field shapes | KML/KMZ, GeoJSON (`.geojson`, `.json`), zipped Shapefile (`.zip`), GeoPackage (`.gpkg`), FlatGeoBuf (`.fgb`) |
| Points | CSV with coordinate columns |
| Point clouds | LAS/LAZ |
| Images | JPEG, PNG |

Files can be up to 5 GB. If an upload is interrupted, Ingabe offers to resume it. Progress shows in the layer panel under **UPLOADING**.

### Add documents for Sage
Reports, policies or guides Sage can read and quote: PDF, DOCX, XLSX, PPTX, TXT, MD or CSV, up to 50 MB. They go into the Knowledge Brain ([Section 16](#16-the-knowledge-brain)), not onto the map.

Satellite imagery, weather, soil and other Rwanda data are not added from this menu: ask Sage, and it adds them to the map for you.

---

## 6. Working with Layers

| Action | How | Saved? |
|--------|-----|--------|
| Rename | Click the layer name and type | Yes |
| Hide / show | Hover over the layer's symbol; it becomes an eye icon | No, only in your browser tab |
| Transparency | Hover over the layer; drag the slider (0–100%) | Yes |
| Colour | **…** menu → **Change color** (shapes only) | Yes |
| Zoom to layer | **…** menu → **Zoom to layer** | — |
| See attributes | **…** menu → **View attributes** | — |
| Delete | **…** menu → **Delete layer**. There is no confirmation step. | — |

**Attributes table:** 100 features per page, with **Previous** / **Next**. Empty values show as *null*. Press Esc to close it.

To change how a layer is coloured beyond one colour (for example, colour fields by NDVI), ask Sage: *"colour the sectors layer by NDVI"*.

---

## 7. Sage, Your Assistant

Ask Sage in plain language. It finds the right data, runs the analysis, adds layers to the map when there is something to see, and answers in words.

### Good questions
- *"Show me Huye"*: draws the district boundary.
- *"List the sectors in Nyanza"* / *"How many villages does Ruhashya have?"*
- *"How green is Gatsibo right now?"*
- *"Will it rain in Musanze this week?"*
- *"Is Kayonza in drought?"*
- *"How healthy is my field in Farm_A_Orthophoto?"*
- *"Does the drought insurance pay out for maize in Bugesera?"*

### Tips
- **Name the place, or select it on the map.** Sector and cell names repeat in different districts; add the district (*"Busasamana in Nyanza"*).
- **Ask for your view.** Insurance reports can be written for a farmer, an insurer, an agronomist or a scientist: *"explain it for a farmer"*. Without that, Sage uses your organization's default view.
- **Ask where a number comes from.** Sage names its data source and you can ask for the date of the data.
- **Some things Sage cannot do:** count buildings on the basemap, identify which crop is growing, or draw buffers and clip layers. Ask it what it can do instead.

---

## 8. Drone Images

Upload your orthophoto ([Section 5](#5-adding-data)), then ask Sage about it by name or say *"this image"*. What Sage can do depends on what the file contains, which it works out from the bands and the file name:

| Your file | What Sage can do |
|-----------|-----------------|
| **Any raster** | Describe it: area in hectares, bands, capture date, and a check that it is placed in the right location. Read the value at a point, the spread of values, or statistics inside a field shape. |
| **Ordinary colour photo (RGB)** | Estimate greenness and crop cover with GRVI, a colour-only index (this is *not* NDVI). Outline likely objects (houses, roads, trees) as a screening mask to review, not a final count. Group the image into hexagon cells that flag areas to inspect. |
| **NDVI or NDRE raster** | Give a plain-language health verdict for the crop and stage, find clusters of stressed plants, compare two flights (with the rainfall between them), and score an insurance trigger from two flights (triggers at 60 out of 100). |
| **Elevation model (DEM/DSM)** | Describe it and read values. |
| **Multispectral (5+ bands)** | Describe it and read values. Sage asks which band is which; computing NDVI from separate bands is not available yet. |

---

## 9. Vegetation from Satellites

Vegetation data comes from **Sentinel-2 L2A** satellite images (10 m) through **Digital Earth Africa** and the **Earth Search** catalog.

### Indices

| Index | Measures |
|-------|----------|
| **NDVI** | Overall vegetation health |
| **EVI** | Vegetation where the canopy is dense |
| **NDWI** | Water in plants and soil |
| **SAVI** | Vegetation where soil shows through |
| **NDRE** | Chlorophyll and nitrogen |
| **NDBI** | Built-up areas |
| **NBR** | Burned areas |

### Reading NDVI

| NDVI | Usually means |
|------|---------------|
| below 0 | Water |
| 0 – 0.2 | Bare soil |
| 0.2 – 0.4 | Sparse or young vegetation |
| 0.4 – 0.6 | Moderate vegetation |
| 0.6 – 0.8 | Healthy, dense vegetation |
| above 0.8 | Very dense vegetation |

### Ask Sage
- *"NDVI for Gasabo district"*: the weekly district average.
- *"Which sectors of Huye have the lowest NDVI?"*: sector and cell values.
- *"How is the vegetation in this field over the last 30 days?"*: daily values for a field or any shape on the map.
- *"Map NDVI over Nyagatare"*: a coloured index layer from the clearest recent image.
- *"Find cloud-free Sentinel-2 images of Musanze this month"* / *"Show a true-colour satellite image of Rubavu"*.

---

## 10. Weather, Drought and Crop Risk

| Ask about | What Sage uses |
|-----------|----------------|
| **Past weather** (*"How much rain fell in Huye last month?"*) | Copernicus AgERA5 (about 10 km), which arrives about 8 days late; the most recent days are filled from Open-Meteo |
| **Forecast** (*"Will it rain in Ngoma this weekend?"*) | ECMWF, GFS, ICON and GraphCast combined, 1–16 days ahead, with a short risk summary |
| **Dry spells** (*"Any dry spells in Bugesera this season?"*) | Days below 2 mm of rain for 10 days or more, from AgERA5 |
| **Drought now** (*"Is Kayonza in drought?"*) | Vegetation Condition Index plus NDWI, per district: normal, watch, moderate or severe drought |
| **Vegetation alerts** (*"Any crop stress alerts?"*) | NDVI that dropped well below its recent normal |
| **Crop stage** (*"Is maize flowering in Musanze yet?"*) | The shape of the NDVI curve this season: dormant, green-up, peak, senescence or harvest |
| **Yield risk** (*"Is the harvest at risk in Gatsibo?"*) | The trend in NDVI over time. It flags risk; it is **not** a yield estimate in tonnes. |

---

## 11. Insurance

Ask *"agricultural situation report for Bugesera"* or *"is the parametric trigger hit for maize in Huye?"*. Sage puts together:

- CHIRPS rainfall for the season so far, and the SPI drought index
- Vegetation (NDVI) and radar, evapotranspiration and soil moisture
- Dry spells and the season's progress (Season A from 15 September, Season B from 15 February)
- Trigger status and the forecast

It works for a district, sector, cell or village, and can compare places.

*"How reliable is the weather data for insurance in Nyagatare?"* gives a confidence rating that checks rainfall detection against observations, dry-spell detection, and whether rainfall and vegetation agree.

For your own drone flights, see the insurance trigger in [Section 8](#8-drone-images).

---

## 12. Soil and Water

**Soil (iSDAsoil, 30 m, about 2020).** Ask *"soil properties at -1.95, 30.10"* or select a field. Topsoil (0–20 cm) and subsoil (20–50 cm). Available: pH, nitrogen, phosphorus, potassium, calcium, magnesium, iron, sulphur, zinc, aluminium, organic carbon, total carbon, clay, sand, silt, bulk density, cation exchange capacity, stone content, depth to bedrock, texture class and fertility class. By default Sage reports pH, nitrogen, phosphorus, potassium, organic carbon, clay, sand and texture.

| Soil pH | Meaning |
|---------|---------|
| below 4.5 | Extremely acidic; most crops struggle |
| 4.5 – 5.5 | Strongly acidic; liming usually helps |
| 5.5 – 6.5 | Moderately acidic; good for most crops |
| 6.5 – 7.5 | Neutral |
| above 7.5 | Alkaline; some nutrients are locked up |

**Soil moisture and water use (FAO WaPOR v3, 100 m, every 10 days).** Ask *"soil moisture here this month"* or *"how much water are crops using in Kirehe?"* (evapotranspiration).

---

## 13. Radar: Through the Clouds

Sentinel-1 radar sees through clouds:

- *"Estimate NDVI here from radar"*: when clouds hide the field, Sage estimates NDVI from the last 30 days of radar.
- *"Map flooded areas in Nyabihu after the rains"*: compares radar before and after, leaves out permanent water, and adds the flooded area to the map.

---

## 14. Land Cover

Land cover comes from the **ESRI / Impact Observatory 10 m annual land cover (2024)**, with the classes water, trees, flooded vegetation, crops, built area, bare ground, snow/ice, clouds and rangeland.

- *"How much cropland is in Bugesera?"*
- *"Add the land cover map for this area"* (all classes, or cropland only)

---

## 15. Rwanda's Districts, Sectors, Cells and Villages

Every map has Rwanda's 30 districts, 416 sectors, 2,148 cells and the villages.

- *"Show me Huye"*, *"Show me the sectors of Nyanza"*, *"Show me Southern Province"*: draws the boundaries.
- *"List the cells in Ruhashya, Huye"*, *"How many sectors does Musanze have?"*: exact names and counts. If a sector or cell name exists in more than one district, Sage asks which one you mean. If a name is misspelt, it suggests the closest ones.
- Coordinates or a click: *"Which village is at -2.60, 29.74?"*

---

## 16. The Knowledge Brain

The Brain holds what your organization knows: fields, farmers, policies, claims, seasons, and the documents you add.

- **Add documents:** **+** → **Add documents for Sage** ([Section 5](#5-adding-data)).
- *"What do we know about Cyampirita?"*: searches the Brain.
- *"Which fields are under this policy?"*, *"Who owns the fields in Huye?"*: follows the links between entities.
- *"How has the NDVI of Cyampirita changed?"*: the history of one value, with drops flagged.
- *"Note that harvesting finished today on Kabeza"*: adds a dated note to an entity.

---

## 17. Data Sources and Update Times

| Data | Source | Detail | Updated |
|------|--------|--------|---------|
| District NDVI | Sentinel-2 L2A via Digital Earth Africa | 10 m, weekly district values | Nightly (2 AM UTC) |
| Field (parcel) NDVI | Sentinel-2 L2A via Digital Earth Africa | 10 m | Nightly (5 AM UTC) |
| Other indices and imagery | Sentinel-2 L2A (Earth Search, Digital Earth Africa) | 10–20 m | Searched when you ask |
| Vegetation alerts, yield risk, drought, crop stage | Computed from district NDVI | Per district | Weekly, Mondays (1–4 AM UTC) |
| Past weather | Copernicus AgERA5 (+ Open-Meteo for the latest days) | ~10 km | Daily, about 8 days behind |
| Forecast | ECMWF, GFS, ICON, GraphCast | 9–28 km, up to 16 days | When you ask |
| Rainfall for insurance | CHIRPS v2.0 | ~5 km | When you ask |
| Soil | iSDAsoil | 30 m | Fixed (about 2020) |
| Soil moisture, evapotranspiration | FAO WaPOR v3 | 100 m | Every 10 days |
| Radar | Sentinel-1 RTC (Planetary Computer) | 10 m | When you ask |
| Land cover | ESRI / Impact Observatory | 10 m | Fixed (2024) |
| Boundaries | Rwanda administrative boundaries | District to village | Fixed |

---

## 18. Common Workflows

### Check a field from a drone flight
1. **+** → **Upload drone or map files** and choose the orthophoto.
2. Ask: *"Describe Farm_A_Orthophoto"* to confirm its area, date and location.
3. Ask: *"How healthy is the crop in Farm_A_Orthophoto?"*
4. If it is an NDVI raster: *"Where are the stressed patches?"*, then after the next flight *"What changed between the two flights?"*

### Check a district or sector
1. Ask: *"Show me Gatsibo"*.
2. Ask: *"How green is Gatsibo right now, and which sectors are lowest?"*
3. Ask: *"Is Gatsibo in drought, and will it rain next week?"*

### Insurance review
1. Ask: *"Situation report for maize in Bugesera, for an insurer"*.
2. Ask: *"How reliable is the rainfall record there?"*
3. Ask: *"Compare Bugesera's sectors"* to see which are worst affected.

### Build your organization's memory
1. **+** → **Add documents for Sage** for policies, field reports and guides.
2. Ask Sage questions that use them: *"What does our maize policy say about dry spells?"*
3. Record events as they happen: *"Note that Kabeza was replanted on 3 October"*.

---

## 19. Troubleshooting

| Problem | What to do |
|---------|-----------|
| Map is blank after signing in | Wait a few seconds, then refresh the page |
| Upload fails | Check the file type ([Section 5](#5-adding-data)); Shapefiles must be zipped; files must be under 5 GB |
| "No layers to display." | Add data with **+**, or ask Sage to show a place |
| The connection light is red / Sage does not answer | Check your internet connection and refresh the page |
| "Sign in to continue" | Your session expired; click **Sign in** |
| Sage says the data is not available | Some data has gaps or is not updated yet; ask for the date of the latest data or a nearby place or time |

---

## 20. Glossary

| Term | Meaning |
|------|---------|
| **Basemap** | The background map (satellite, streets or terrain) |
| **Brain** | Ingabe's store of your organization's knowledge and documents |
| **DEM / DSM** | Elevation models of the ground / surface |
| **GeoTIFF** | An image file that knows where on Earth it belongs |
| **GRVI** | A greenness index from ordinary colour photos (not NDVI) |
| **Layer** | One dataset on the map |
| **NDVI** | Normalized Difference Vegetation Index: plant health from 0 (bare) to 1 (dense) |
| **Orthophoto** | A drone image corrected so it lines up with the map |
| **Season A / B** | Rwanda's two main growing seasons (from mid-September and mid-February) |
| **Sentinel-1 / Sentinel-2** | European satellites for radar / optical images |
| **SPI** | Standardized Precipitation Index: how unusual the rainfall is |
| **VCI** | Vegetation Condition Index: this season's vegetation against past years |

---

*Ingabe is developed by NozaLabs.*
