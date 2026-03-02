# Ingabe Training Manual

**A Complete Guide for Farmers, Agronomists, NGOs, and Agricultural Professionals**

---

## Table of Contents

1. [What is Ingabe?](#1-what-is-ingabe)
2. [Getting Started](#2-getting-started)
3. [The Home Screen — Your Projects](#3-the-home-screen--your-projects)
4. [The Map Workspace](#4-the-map-workspace)
5. [Adding Data to Your Map](#5-adding-data-to-your-map)
6. [Working with Layers](#6-working-with-layers)
7. [Styling and Choropleth Maps](#7-styling-and-choropleth-maps)
8. [Sage — Your AI Assistant](#8-sage--your-ai-assistant)
9. [Satellite Imagery and Vegetation Indices](#9-satellite-imagery-and-vegetation-indices)
10. [Soil Analysis](#10-soil-analysis)
11. [Weather and Climate Data](#11-weather-and-climate-data)
12. [Crop Yield Forecasting](#12-crop-yield-forecasting)
13. [Greenhouse Gas Emissions](#13-greenhouse-gas-emissions)
14. [Land Cover Analysis](#14-land-cover-analysis)
15. [Rwanda Agriculture Dashboard](#15-rwanda-agriculture-dashboard)
16. [Geoprocessing Tools](#16-geoprocessing-tools)
17. [Sharing and Collaboration](#17-sharing-and-collaboration)
18. [Common Workflows for Agriculture](#18-common-workflows-for-agriculture)
19. [Troubleshooting](#19-troubleshooting)
20. [Glossary](#20-glossary)

---

## 1. What is Ingabe?

Ingabe is a web-based Geographic Information System (GIS) designed for agricultural monitoring and land management in East Africa. It runs in your web browser — no software installation required.

**What Ingabe helps you do:**

- Upload and visualize farm boundaries, field maps, and spatial data on interactive maps
- Monitor crop health using satellite imagery (NDVI, EVI, and other vegetation indices)
- Analyze soil properties (pH, nitrogen, phosphorus, organic carbon, and 18 more nutrients)
- Track weather patterns (temperature, rainfall, solar radiation)
- Forecast crop yields using the DSSAT crop simulation model
- Monitor greenhouse gas emissions from agricultural activities
- Classify land cover (cropland, forest, built-up areas, rangeland)
- Run spatial analysis tools (buffer zones, intersections, zonal statistics, and 30+ more)
- Chat with **Sage**, an AI assistant that executes GIS operations via natural language

**Who is Ingabe for?**

| User | How Ingabe Helps |
|------|------------------|
| **Farmers** | Monitor crop health, check soil and weather data for your fields |
| **Agronomists** | Analyze vegetation indices, forecast yields, generate field reports |
| **NGOs** | Map project areas, track land use change, create visual reports |
| **Researchers** | Access multi-source geospatial data, run spatial analysis |
| **Government** | District-level agricultural monitoring, food security dashboards |

**Access Ingabe at:** [https://gis.nozalabs.rw](https://gis.nozalabs.rw)

---

## 2. Getting Started

### Creating an Account

1. Open your web browser and navigate to [https://gis.nozalabs.rw](https://gis.nozalabs.rw)
2. Click **Sign Up** and create your account
3. Once logged in, you will see the **Home Screen** with your projects

### System Requirements

- A modern web browser (Chrome, Firefox, Edge, or Safari)
- Internet connection
- No special software or plugins needed

---

## 3. The Home Screen — Your Projects

When you log in, you see **"Your Maps"** — a grid of all your projects.

### What You See

- **Project Cards** — Each card shows a map thumbnail, the project name, and when it was last edited (e.g., "2 hours ago")
- **"+ New Map"** button (top right, green) — Creates a new blank map project
- **"Show recently deleted"** checkbox — Reveals any projects you soft-deleted

### Managing Projects

| Action | How |
|--------|-----|
| **Create a new project** | Click the green **"+ New Map"** button |
| **Open a project** | Click on its card |
| **Rename a project** | Open the project, then click the title text at the top of the layer panel |
| **Delete a project** | Hover over a card and click the trash icon |
| **Recover a deleted project** | Check "Show recently deleted" to see and restore it |

---

## 4. The Map Workspace

When you open a project, you enter the **Map Workspace** — the main working area of Ingabe.

### Layout Overview

The workspace has four main areas:

```
+------------------------------------------------------------------+
|  [Sidebar]  |  Layer Panel  |      Map View      |  Chat Panel   |
|             |               |                     |  (Sage AI)    |
|  Home       |  Map Title    |                     |               |
|  Projects   |  Layer List   |    Interactive      | Previous chats|
|             |  Zoom / Add   |    Globe Map        |               |
|             |               |                     | Chat input    |
+------------------------------------------------------------------+
```

### Left Sidebar (collapsed by default)

- **Ingabe logo** — Click to expand the sidebar
- **Home icon** — Return to the projects list
- **Project list** — Switch between your projects
- Click the logo again to collapse the sidebar

### Layer Panel (left side of map)

- **Map Title** — Editable text field. Click to rename your map
- **Share icon** — Opens sharing options
- **Layer List** — Shows all layers on your map. "No layers to display." when empty
- **Zoom controls** — Navigate between layers with Previous/Next arrows
- **"+" button** — Opens the menu to add data (see [Section 5](#5-adding-data-to-your-map))
- **Database icon** — Connect to a PostGIS database

### Map View (center)

- **Interactive globe/map** — Pan by clicking and dragging. Scroll to zoom
- **Zoom buttons** (+/−) — Top-right corner
- **North arrow** — Click to reset map rotation
- **Basemap switcher** — Bottom-right globe icon. Choose from:
  - **Satellite** — High-resolution aerial imagery (Esri/Maxar)
  - **OpenStreetMap** — Street-level detail
  - **OpenFreeMap** — Clean, modern map style
  - **Topographic** — Shows terrain and elevation contours
  - **Dark Matter** — Dark background for data overlay
  - **Voyager** — Balanced between detail and clarity
- **Scale bar** — Shows current map scale (e.g., "1000 km")
- **Attribution** — Map data credits

### Chat Panel (right side — "Sage")

- **Previous chats** — Dropdown to view conversation history
- **Search icon** — Search through past conversations
- **Chat messages** — Shows your conversation with Sage
- **Chat input** — "Type in for Sage to do something..." — Ask Sage to perform GIS tasks

---

## 5. Adding Data to Your Map

Click the **"+"** button in the Layer Panel to see your options:

### Upload File

Upload data directly from your computer. Supported formats:

| Format | Extension | Type | Common Use |
|--------|-----------|------|------------|
| GeoJSON | `.geojson`, `.json` | Vector | Web maps, field boundaries |
| Shapefile | `.zip` (zipped) | Vector | Traditional GIS data |
| GeoPackage | `.gpkg` | Vector | Modern GIS data |
| KML/KMZ | `.kml`, `.kmz` | Vector | Google Earth data |
| FlatGeoBuf | `.fgb` | Vector | Fast streaming vector |
| CSV | `.csv` | Tabular | Spreadsheets with coordinates |
| GeoTIFF | `.tif`, `.tiff` | Raster | Satellite imagery, DEMs |
| LAZ/LAS | `.laz`, `.las` | Point Cloud | 3D terrain surveys |

**How to upload:**
1. Click the **"+"** button
2. Select **"Upload file"**
3. Choose a file from your computer (or drag and drop onto the map area)
4. Ingabe processes the file and adds it as a layer

### Add Remote URL

Add data from a web link (e.g., a GeoJSON or GeoTIFF hosted online):
1. Click **"+"** > **"Add remote URL"**
2. Paste the URL
3. Ingabe downloads and adds the data

### Connect to WFS

Connect to a Web Feature Service for live spatial data:
1. Click **"+"** > **"Connect to WFS"**
2. Enter the WFS server URL
3. Browse and select available layers

### Google Sheets

Import data from a shared Google Sheets spreadsheet:
1. Click **"+"** > **"Google Sheets"**
2. Paste the Google Sheets URL
3. Ingabe converts it to a spatial layer (requires latitude/longitude columns)

### ESRI Feature Service

Connect to ArcGIS Online or ArcGIS Server services:
1. Click **"+"** > **"ESRI Feature Service"**
2. Enter the ESRI service URL
3. Select the layers to add

### PostGIS Database

Connect to an external PostgreSQL/PostGIS database:
1. Click the **database icon** in the Layer Panel
2. Enter connection details:
   - **URI** format: `postgresql://user:password@host:port/database`
   - Or fill in individual fields: Host, Port, Database, Username, Password
3. Browse available tables and add them as layers

---

## 6. Working with Layers

Once data is on your map, each layer appears in the Layer Panel with its name and controls.

### Layer Controls

| Action | How | What It Does |
|--------|-----|--------------|
| **Toggle visibility** | Click the eye icon | Show/hide a layer on the map |
| **Adjust opacity** | Use the opacity slider | Make a layer more or less transparent |
| **Change color** | Click the color swatch | Pick a new fill color for vector layers |
| **Zoom to layer** | Click "Zoom" | Fit the map to show the entire layer extent |
| **View attributes** | Click the table icon | Open the attribute table showing all data fields |
| **Rename** | Click the layer name | Edit the layer name inline |
| **Delete** | Click the trash icon | Remove the layer from the map |
| **Reorder** | Drag layers up/down | Change the drawing order (top layer draws last) |

### Attribute Table

Click the **table icon** next to a layer to open its attribute table:

- **Paginated view** — Shows 100 features per page
- **All columns** — Displays every attribute field in the data
- **Navigation** — Use Previous/Next to browse through large datasets
- **Feature count** — Shows total number of features (e.g., "Showing 1-100 of 2,456 features")

### Layer Types

Ingabe automatically detects and handles different data types:

- **Vector layers** (points, lines, polygons) — Drawn as styled features on the map
- **Raster layers** (imagery, GeoTIFF) — Displayed as image tiles
- **Point cloud layers** (LAZ/LAS) — Rendered in 3D perspective view
- **PostGIS layers** — Live data from a connected database

---

## 7. Styling and Choropleth Maps

### Quick Styling

For any vector layer, you can change:
- **Fill color** — Click the color swatch next to the layer name
- **Opacity** — Use the slider to control transparency

### Choropleth Maps (Color by Value)

A choropleth map colors each feature based on a data value — for example, coloring districts by average NDVI or soil pH.

**How to create a choropleth:**

1. Right-click on a layer (or use the layer menu)
2. Select **"Choropleth"**
3. Choose the **column** to visualize (e.g., "ndvi_mean", "ph", "yield_forecast_tha")
4. Select a **classification method**:
   - **Quantiles** — Equal number of features in each class
   - **Jenks (Natural Breaks)** — Groups by natural data clusters
5. Choose the **number of classes** (e.g., 5)
6. Select a **color palette**:
   - Sequential: Blues, Reds, Greens, Oranges, Purples
   - Diverging: Red-Yellow-Green, Red-Blue
   - Perceptual: Viridis, Plasma
7. Click **Apply** — The map updates instantly

**Enriching data for choropleths:**

If your layer doesn't have the data column you want to visualize, you can **enrich** it first. In the Choropleth dialog, click **"Compute"** next to a metric to calculate values like NDVI, soil pH, or crop yield for each feature.

### Pie Charts for Buffer Layers

When working with a **single-feature layer** (such as a buffer zone), Ingabe shows a **pie chart** on the map instead of a solid color. This displays the proportional breakdown of land cover types (cropland, forest, built area, rangeland) within that area.

### AI-Powered Styling

You can also ask Sage to style your layers using natural language:
- "Make this layer green with thick borders"
- "Color the districts by population"
- "Style the farms with a red-to-green gradient based on NDVI"

---

## 8. Sage — Your AI Assistant

**Sage** is Ingabe's built-in AI assistant that can execute GIS operations through natural language conversation.

### How to Use Sage

1. Type your request in the chat input: **"Type in for Sage to do something..."**
2. Press Enter
3. Sage processes your request, runs the appropriate tools, and shows the results

### What Sage Can Do

**Data Operations:**
- "Show me the districts of Rwanda on the map"
- "Add Rwanda district boundaries"
- "Create a point at longitude 29.87, latitude -1.94"

**Satellite and Vegetation Analysis:**
- "What is the NDVI in Gasabo district?"
- "Show me vegetation health for this area"
- "Get agricultural indices for Kigali"
- "Search for Sentinel-2 imagery from January 2025"

**Soil Analysis:**
- "What is the soil pH at this location?"
- "Get soil properties: nitrogen, phosphorus, and organic carbon"

**Weather Data:**
- "What was the rainfall in Musanze last month?"
- "Show weather statistics for Huye district"

**Land Cover:**
- "Add the land cover map for this area"
- "What percentage of this area is cropland?"
- "Show me the largest cropland areas in Nyagatare"

**Geoprocessing:**
- "Create a 5 km buffer around this point"
- "Clip this layer using Rwanda boundaries"
- "Calculate zonal statistics for NDVI within these polygons"
- "Merge these two layers together"
- "Dissolve the boundaries by province"

**Anomaly Detection:**
- "Are there any crop stress alerts?"
- "Show me drought status for all districts"
- "What is the yield risk in Bugesera?"

**Emissions:**
- "Show methane emissions from agriculture in 2022"
- "Compare N2O emissions across districts"

### Tips for Talking to Sage

- **Be specific** — "Show NDVI for Gasabo district" works better than "show me vegetation"
- **One task at a time** — Complete one request before making the next
- **Sage sees your map** — If you have a layer visible, Sage uses its location automatically
- **Sage executes immediately** — It will run tools right away, not just describe what to do

### Data Sources Sage Uses

| Data | Source | Resolution | Coverage |
|------|--------|-----------|----------|
| Vegetation indices | Sentinel-2 via Sentinel Hub | 10-20m | Global |
| Soil properties | iSDAsoil | 30m | Africa |
| Weather | Copernicus AgERA5 / Open-Meteo | ~11 km | Global |
| Land cover | ESRI 10m LULC | 10m | Global |
| Emissions | EDGAR v8.0 (JRC) | ~11 km | Global |
| Crop yield | DSSAT + Sentinel-2 | Per-field | East Africa |
| Administrative boundaries | Rwanda NISR | Vector | Rwanda |

---

## 9. Satellite Imagery and Vegetation Indices

Ingabe connects to the Copernicus Sentinel Hub to provide real-time satellite-based vegetation analysis.

### Available Indices

| Index | Full Name | What It Measures | Healthy Range |
|-------|-----------|-----------------|---------------|
| **NDVI** | Normalized Difference Vegetation Index | Overall vegetation health | 0.4 - 0.8 |
| **EVI** | Enhanced Vegetation Index | Vegetation in dense canopy | 0.3 - 0.7 |
| **NDWI** | Normalized Difference Water Index | Water/moisture content | > 0.0 |
| **SAVI** | Soil-Adjusted Vegetation Index | Vegetation on bare soil | 0.3 - 0.7 |
| **NDRE** | Normalized Difference Red Edge Index | Nitrogen/chlorophyll | > 0.2 |
| **NDBI** | Normalized Difference Built-up Index | Urban/built-up areas | > 0.0 |

### Understanding NDVI Values

NDVI is the most commonly used index for crop monitoring:

| NDVI Range | Interpretation | Color |
|-----------|----------------|-------|
| < 0.0 | Water or snow | Blue |
| 0.0 - 0.2 | Bare soil, no vegetation | Red |
| 0.2 - 0.4 | Sparse vegetation, early growth | Orange |
| 0.4 - 0.6 | Moderate vegetation | Yellow |
| 0.6 - 0.8 | Healthy, dense vegetation | Green |
| > 0.8 | Very healthy, peak growth | Dark Green |

### How to Get Satellite Data

**Via Sage:**
- "Get field health for this area" (uses your visible layer's location)
- "Show NDVI for Gasabo district over the last 30 days"

**Via Layer Enrichment:**
- Open the Choropleth dialog on a vector layer
- Select an NDVI/EVI/NDWI metric
- Click "Compute" to calculate average values for each feature

---

## 10. Soil Analysis

Ingabe accesses **iSDAsoil** — a 30-meter resolution soil dataset covering all of Africa, created using machine learning on thousands of soil samples.

### Available Soil Properties (21 total)

**Nutrients:**
- Nitrogen (g/kg), Phosphorus (ppm), Potassium (ppm)
- Calcium, Magnesium, Iron, Sulphur, Zinc, Aluminium

**Organic Matter:**
- Organic Carbon (g/kg), Total Carbon (g/kg)

**Physical Properties:**
- Clay Content (%), Sand Content (%), Silt Content (%)
- Bulk Density (g/cm3), Stone Content (%), Bedrock Depth (cm)

**Chemical Properties:**
- pH (0-14), Cation Exchange Capacity (cmol+/kg)

**Classification:**
- USDA Texture Class (e.g., "Sandy Loam", "Clay")

**Depth Options:** 0-20 cm (topsoil) and 20-50 cm (subsoil)

### How to Get Soil Data

**Via Sage:**
```
"What is the soil pH and nitrogen at longitude 29.87, latitude -1.95?"
```

**Via Layer Enrichment:**
- Open the Choropleth dialog on a polygon layer
- Select a soil metric (e.g., "pH", "Nitrogen", "Organic Carbon")
- Click "Compute" to calculate soil values at each polygon's center

### Interpreting Soil pH

| pH Range | Classification | Implication |
|---------|---------------|-------------|
| < 4.5 | Extremely acidic | Most crops struggle |
| 4.5 - 5.5 | Strongly acidic | Liming recommended |
| 5.5 - 6.5 | Moderately acidic | Good for most crops |
| 6.5 - 7.5 | Neutral | Ideal for agriculture |
| 7.5 - 8.5 | Alkaline | Some nutrient lockout |

---

## 11. Weather and Climate Data

Ingabe provides weather data from two sources:

- **Copernicus AgERA5** — High-quality reanalysis data (0.1 degree resolution, ~11 km)
- **Open-Meteo** — Near real-time observations (updates daily)

### Available Weather Variables

| Variable | Unit | Source |
|----------|------|--------|
| Temperature (mean/min/max) | Celsius | AgERA5 |
| Precipitation | mm/day | AgERA5 |
| Solar Radiation | MJ/m2/day | AgERA5 |
| Wind Speed | m/s | Open-Meteo |

### How to Get Weather Data

**Via Sage:**
```
"What was the rainfall in Musanze district last week?"
"Show temperature trends for Huye from January to March"
```

**Via Layer Enrichment:**
- Select weather metrics (Rainfall, Temperature, Wind Speed)
- Values represent the last 10 days average from Open-Meteo

---

## 12. Crop Yield Forecasting

Ingabe uses the **DSSAT crop simulation model** combined with satellite data assimilation to forecast crop yields.

### How It Works

1. **Soil Profile** — Queries iSDAsoil for your field's soil hydraulic properties
2. **Weather Data** — Retrieves daily temperature, rainfall, and solar radiation from NASA POWER
3. **Crop Calendar** — Uses Rwanda-specific planting dates and management practices
4. **DSSAT Simulation** — Runs a crop growth model to estimate potential yield
5. **Satellite Correction** — Adjusts the forecast using real Sentinel-2 NDVI observations from your field

### Supported Crops (Rwanda)

| Crop | Season A | Season B |
|------|----------|----------|
| Maize | Sep - Feb | Feb - Jul |
| Rice | Sep - Feb | Feb - Jul |
| Beans | Sep - Jan | Feb - Jun |
| Sorghum | Sep - Feb | Feb - Jul |
| Wheat | Feb - Jul (marshlands) | — |

### How to Get Yield Forecasts

**Via Sage:**
```
"What is the yield forecast for maize at this location?"
```

**Via Layer Enrichment:**
- Select the "Yield Forecast (t/ha)" metric
- Click "Compute" to run DSSAT for each polygon in your layer

---

## 13. Greenhouse Gas Emissions

Ingabe accesses **EDGAR v8.0** (Emissions Database for Global Atmospheric Research) from the European Commission's Joint Research Centre.

### Available Emissions Data

| Gas | Symbol | Agricultural Sources |
|-----|--------|---------------------|
| Methane | CH4 | Livestock, rice paddies, manure, burning |
| Nitrous Oxide | N2O | Fertilizer, manure, crop residues |
| Carbon Dioxide | CO2 | Agricultural soils |
| Ammonia | NH3 | Fertilizer, manure, burning |

### Agricultural Sectors

| Code | Sector | Description |
|------|--------|-------------|
| AGS | Agricultural Soils | Emissions from fertilizer use and crop residues |
| ENF | Enteric Fermentation | Methane from livestock digestion |
| MNM | Manure Management | Emissions from animal waste |
| AWB | Agricultural Waste Burning | Emissions from crop residue burning |

### How to Get Emissions Data

**Via Sage:**
```
"Show methane emissions from agriculture in Nyagatare district"
"Compare N2O emissions across all districts for 2022"
```

**Via Layer Enrichment:**
- Select CH4, N2O, or CO2 emissions metric
- Values are in tonnes per year per grid cell

---

## 14. Land Cover Analysis

Ingabe uses the **ESRI 10-meter Annual Land Use/Land Cover** dataset to classify land cover types.

### Land Cover Classes

| Class | Color | Description |
|-------|-------|-------------|
| Cropland | Yellow | Active farmland and crop areas |
| Forest | Green | Tree canopy > 10m height |
| Built Area | Red | Urban, roads, infrastructure |
| Rangeland | Tan | Grasslands and shrublands |
| Water | Blue | Rivers, lakes, reservoirs |
| Bare Ground | Brown | Exposed soil, rock, sand |

### How to Analyze Land Cover

**Via Sage:**
```
"Add the land cover map for Kigali"
"What percentage of Bugesera is cropland?"
"Show me the largest cropland areas near this point"
```

**Via Layer Enrichment:**
- Select land cover metrics: Cropland %, Forest %, Built Area %, Rangeland %
- For single-feature layers (buffer zones), a **pie chart** appears on the map showing the proportional breakdown

---

## 15. Rwanda Agriculture Dashboard

Ingabe includes a specialized **Rwanda Agriculture Dashboard** for national-level crop monitoring.

### Accessing the Dashboard

Click the **Dashboard icon** in the left sidebar (house icon), or navigate directly.

### Dashboard Features

**District Selection:**
- Choose from all 30 districts in Rwanda
- Data updates instantly when you switch districts

**NDVI Hexagonal Grid Map:**
- The map shows Rwanda divided into **H3 hexagonal cells**
- Each cell is colored by its average NDVI value:
  - Red = low vegetation (bare soil, stress)
  - Yellow = moderate vegetation
  - Green = healthy vegetation
- Hover over a hexagon to see its NDVI value

**District Statistics Cards:**

| Card | What It Shows |
|------|---------------|
| **Agricultural Parcels** | Total number of farm plots in the district |
| **Average NDVI** | Current average vegetation health with trend arrow (up, down, stable) |
| **Yield Risk** | Risk level (Low/Medium/High) with confidence percentage |

**NDVI Time Series Chart:**
- Area chart showing how vegetation health changes over time
- Reference lines at:
  - 0.2 — Bare soil threshold (red dashed line)
  - 0.6 — Healthy vegetation threshold (green dashed line)
- Color gradient from red (low) through yellow (moderate) to green (healthy)

**ML-Powered Recommendations:**
- AI-generated suggestions based on current conditions
- Example: "Consider irrigation in high-risk areas" or "Vegetation trends declining — investigate potential drought stress"

**Historical Summary:**
- Minimum, Mean, and Maximum NDVI values over the time period

---

## 16. Geoprocessing Tools

Ingabe includes 35+ spatial analysis tools, all accessible through Sage or the geoprocessing interface.

### Vector Operations

| Tool | What It Does | Example Use |
|------|-------------|-------------|
| **Buffer** | Creates a zone around features at a specified distance | "Create a 5 km buffer around this well" |
| **Clip** | Cuts a layer to fit within a boundary | "Clip farms to district boundary" |
| **Intersection** | Finds overlapping areas between layers | "Where do farms overlap with forest?" |
| **Union** | Combines two layers into one | "Merge northern and southern districts" |
| **Dissolve** | Merges features that share an attribute | "Dissolve sectors into districts" |
| **Spatial Join** | Transfers attributes between layers based on location | "Add district names to farm points" |
| **Merge Layers** | Combines multiple layers into one | "Merge all uploaded farm boundaries" |
| **Field Calculator** | Adds a new column with a computed formula | "Calculate area in hectares" |

### Analysis Tools

| Tool | What It Does | Example Use |
|------|-------------|-------------|
| **Zonal Statistics** | Calculates raster statistics within polygon zones | "Average NDVI per district" |
| **Create Grid** | Generates a regular grid (hexagonal, rectangular) | "Create a hex grid over Kigali" |
| **Statistics by Categories** | Groups and summarizes data by a category field | "Count farms per crop type" |
| **Aggregate** | Summarizes features by an expression | "Total area per province" |

### Raster Operations

| Tool | What It Does |
|------|-------------|
| **Reproject** | Change the coordinate system of a raster |
| **Zonal Statistics** | Calculate min/max/mean/sum of raster values within polygons |

### Using Geoprocessing Tools

All tools can be invoked through Sage:

```
You: "Create a 10 km buffer around Kigali city center"
Sage: [Runs buffer tool] → New buffer layer appears on map

You: "Calculate zonal statistics of NDVI within these farm boundaries"
Sage: [Runs zonal stats] → Each farm gets mean, min, max NDVI values
```

---

## 17. Sharing and Collaboration

### Share a Map

1. Click the **share icon** in the Layer Panel header
2. Choose your sharing option:
   - **Copy link** — Share a URL that others can view
   - **Embed** — Get an HTML iframe code to embed in websites

### Connecting External Databases

For organizations with their own spatial data:

1. Click the **database icon** in the Layer Panel
2. Enter your PostGIS connection details
3. Browse and add tables directly to your map
4. Data stays live — changes in your database reflect on the map

---

## 18. Common Workflows for Agriculture

### Workflow 1: Monitor Crop Health in Your District

1. Open Ingabe and create a new map
2. Ask Sage: **"Add Rwanda district boundaries"**
3. Ask Sage: **"What is the NDVI in [your district]?"**
4. Review the vegetation health results
5. Ask Sage: **"Are there any crop stress alerts in [your district]?"**
6. Check the Rwanda Dashboard for time series trends

### Workflow 2: Analyze a Specific Farm

1. **Upload your farm boundary** (GeoJSON, Shapefile, or KML)
2. Ask Sage: **"Get field health for this area"**
3. Ask Sage: **"What is the soil pH and nitrogen at this location?"**
4. Ask Sage: **"What is the yield forecast for maize at this location?"**
5. Review the satellite imagery, soil data, and yield prediction

### Workflow 3: Land Use Mapping for an NGO Project

1. Upload your **project area boundary**
2. Ask Sage: **"Add the land cover map for this area"**
3. Ask Sage: **"What percentage of this area is cropland?"**
4. Open the **Choropleth dialog** to color areas by land cover percentage
5. **Share the map** with stakeholders via link

### Workflow 4: Environmental Impact Assessment

1. Upload your **area of interest**
2. Ask Sage: **"Create a 5 km buffer around this area"**
3. Ask Sage: **"Show methane and N2O emissions for this area"**
4. Enrich the buffer with **land cover, soil, and weather data**
5. Export or share the results

### Workflow 5: District-Level Agriculture Report

1. Open the **Rwanda Dashboard**
2. Select your **district**
3. Review:
   - Current NDVI and trend direction
   - Yield risk assessment
   - Historical NDVI time series
   - ML-generated recommendations
4. Switch to the map view for spatial detail
5. Share the dashboard with your team

---

## 19. Troubleshooting

### Common Issues

| Issue | Solution |
|-------|----------|
| **Map is blank after login** | Wait a few seconds for the map to load. Try refreshing the page |
| **Layer upload fails** | Check file format is supported. Shapefiles must be in a .zip archive |
| **"No layers to display"** | Add data using the "+" button or ask Sage |
| **Sage doesn't respond** | Check your internet connection. Try refreshing the page |
| **Choropleth shows no colors** | Make sure the column has numeric values. Click "Compute" to generate metric data first |
| **Map loads slowly** | Large raster files take longer. Try zooming in to a specific area |
| **"Exceeded memory limit" error** | The server is restarting. Wait 1-2 minutes and refresh |

### Getting Help

- Type questions to **Sage** — it can explain features and guide you
- Contact support through [NozaLabs](https://app.nozalabs.rw)

---

## 20. Glossary

| Term | Definition |
|------|-----------|
| **Basemap** | The background map (satellite, street map, or topographic) |
| **Buffer** | A zone created at a set distance around a feature |
| **Choropleth** | A map that colors areas based on data values |
| **COG** | Cloud-Optimized GeoTIFF — a raster image format for web maps |
| **CRS** | Coordinate Reference System — how coordinates map to real locations |
| **DSSAT** | Decision Support System for Agrotechnology Transfer — crop simulation model |
| **EDGAR** | Emissions Database for Global Atmospheric Research |
| **EVI** | Enhanced Vegetation Index |
| **Feature** | A single geographic object (a point, line, or polygon) with attributes |
| **GeoJSON** | A standard format for encoding geographic features as JSON |
| **GIS** | Geographic Information System — software for analyzing spatial data |
| **H3** | A hexagonal spatial indexing system used for grid analysis |
| **iSDAsoil** | Innovative Solutions for Decision Agriculture — Africa soil dataset |
| **Layer** | A dataset displayed on the map (e.g., district boundaries, farm plots) |
| **LULC** | Land Use / Land Cover classification |
| **MVT** | Mapbox Vector Tiles — efficient format for serving vector data |
| **NDVI** | Normalized Difference Vegetation Index — measures plant health |
| **NDWI** | Normalized Difference Water Index — measures water content |
| **PMTiles** | A cloud-optimized format for vector tiles |
| **PostGIS** | A spatial extension for PostgreSQL databases |
| **Raster** | Grid-based spatial data (pixels), like satellite imagery |
| **Sage** | Ingabe's AI assistant for natural language GIS operations |
| **Sentinel-2** | European Space Agency satellite providing 10m imagery every 5 days |
| **Shapefile** | A common GIS file format (must be zipped for upload) |
| **Vector** | Coordinate-based spatial data (points, lines, polygons) |
| **WFS** | Web Feature Service — a standard for serving vector data over the web |
| **Zonal Statistics** | Calculating raster values (mean, min, max) within polygon boundaries |

---

*Ingabe is developed by NozaLabs. For more information, visit [app.nozalabs.rw](https://app.nozalabs.rw).*
