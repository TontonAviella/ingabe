# geoBoundaries: Rwanda administrative boundaries (vendored)

These files seed the `rwanda_{district,sector,cell,village}_boundaries` tables
in the Alembic migrations `e1f2a3b4c5d6`, `f2a3b4c5d6e7` and `b2c3d4e5f6a7`,
read through `src/database/geoboundaries.py`. They are vendored so that
migrations never depend on a third-party website being reachable.

| File | Level | Units | Original source (per geoBoundaries metadata) |
|---|---|---|---|
| `geoBoundaries-RWA-ADM2.geojson.xz` | ADM2 districts | 30 | Open Data Rwanda (www.statistics.gov.rw/terms-use) |
| `geoBoundaries-RWA-ADM3.geojson.xz` | ADM3 sectors | 416 | Open Data Rwanda (www.statistics.gov.rw/terms-use) |
| `geoBoundaries-RWA-ADM4.geojson.xz` | ADM4 cells | 2,148 | Open Data Rwanda (www.statistics.gov.rw/terms-use) |
| `geoBoundaries-RWA-ADM5_simplified.geojson.xz` | ADM5 villages, simplified geometry | 14,815 | The World Bank (datacatalog.worldbank.org/search/dataset/0041453) |

Each file is the unmodified gbOpen release file from
`https://github.com/wmgeolab/geoBoundaries/raw/9469f09/releaseData/gbOpen/RWA/<LEVEL>/`
(build date Dec 12, 2023; boundary year 2012; the commit the geoBoundaries API
`gjDownloadURL` pointed to on 2026-10-04), compressed with `xz -9e`.
`src/database/geoboundaries.py` pins the SHA-256 of each decompressed file and
refuses a file that does not match.

## Licence and attribution

All four files are licensed **Creative Commons Attribution 4.0 International
(CC BY 4.0)**, https://creativecommons.org/licenses/by/4.0/. No changes were
made to the data other than compression.

Attribution:

> Runfola, D. et al. (2020) geoBoundaries: A global database of political
> administrative boundaries. PLoS ONE 15(4): e0231866.
> https://doi.org/10.1371/journal.pone.0231866. Data: geoBoundaries
> (www.geoboundaries.org), William & Mary geoLab; ADM2–ADM4 from Open Data
> Rwanda / National Institute of Statistics of Rwanda; ADM5 from The World Bank.

## Updating

Download the new release files, compress them with `xz -9e`, replace the files
here, and update the hashes in `src/database/geoboundaries.py` (SHA-256 of the
decompressed GeoJSON) and the commit and build date above. Existing databases
keep their rows: the seeds skip tables that are already populated.
