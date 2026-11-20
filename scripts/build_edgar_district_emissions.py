"""Rebuild alembic/seeds/edgar_farm_emissions_rwanda_2018_2022.csv from EDGAR's 0.1 degree grids.

Run once on 2026-11-20 (reports/emissions/prereg-edgar-district-rebuild-2026-11-20.md). Not part of the app:
it needs h5py and numpy, and the 55 EDGAR "emi_nc" zip files (about 190 MB) in one folder:
  https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/EDGAR/datasets/v80_FT2022_GHG/{CH4,N2O,CO2}/{sector}/emi_nc/
  https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/EDGAR/datasets/v81_FT2022_AP_new/NH3/{sector}/emi_nc/

It reads two small files from the working folder, both made read-only from PostGIS:
  shares.csv: district, square centre lon, lat, share of the square inside the district's outline, plus the same
    for the union of all districts under the name __RWANDA__:
      with g as (select x/100.0 lon, y/100.0 lat,
                        st_makeenvelope(x/100.0-0.05, y/100.0-0.05, x/100.0+0.05, y/100.0+0.05, 4326) sq
                 from generate_series(2875,3105,10) x, generate_series(-295,-95,10) y),
           d as (select district, st_makevalid(geom) geom from rwanda_district_boundaries)
      select d.district, g.lon, g.lat, st_area(st_intersection(d.geom, g.sq))/0.01
      from d join g on st_intersects(d.geom, g.sq) where st_area(st_intersection(d.geom, g.sq)) > 0
  areas.csv: district, st_area(st_makevalid(geom)) in square degrees, km2.

A district's tonnes = sum over squares of (the square's tonnes x the share of the square inside the outline).
This assumes emissions are spread evenly inside a square. It prints the bars the test fixed beforehand.

Usage: python build_edgar_district_emissions.py <folder of zips> <output csv>
"""
import csv
import glob
import io
import os
import re
import sys
import zipfile
from collections import defaultdict

import h5py
import numpy as np

SECTORS = {"AGS": "Agricultural soils", "ENF": "Enteric fermentation", "MNM": "Manure management",
           "AWB": "Agricultural waste burning"}
NAME = re.compile(r"v8\.(\d)_FT2022_(GHG|AP)_([A-Z0-9]+)_(\d{4})_([A-Z]+)_emi_nc\.zip")


def main(folder: str, out: str) -> None:
    shares = defaultdict(list)
    square_sum = defaultdict(float)
    district_area = defaultdict(float)
    with open("shares.csv", newline="") as fh:
        for district, lon, lat, share in csv.reader(fh):
            lon, lat, share = round(float(lon), 2), round(float(lat), 2), float(share)
            shares[district].append((lon, lat, share))
            if district != "__RWANDA__":
                square_sum[(lon, lat)] += share
                district_area[district] += share * 0.01
    with open("areas.csv", newline="") as fh:
        areas = {r[0]: float(r[1]) for r in csv.reader(fh)}
    print("bar2 max share sum per square:", round(max(square_sum.values()), 5))
    worst_area = max(abs(district_area[d] / areas[d] - 1) for d in areas)
    print("bar3 worst district area gap: %.4f%%" % (100 * worst_area), "districts", len(areas))

    rows, worst_sum, national = [], 0.0, {}
    for path in sorted(glob.glob(os.path.join(folder, "*.zip"))):
        ver, kind, gas, year, sector = NAME.match(os.path.basename(path)).groups()
        archive = zipfile.ZipFile(path)
        grid = h5py.File(io.BytesIO(archive.read(next(n for n in archive.namelist() if n.endswith(".nc")))), "r")
        assert grid["emissions"].attrs["units"] in (b"Tonnes", "Tonnes"), grid["emissions"].attrs["units"]
        lats, lons, values = grid["lat"][:], grid["lon"][:], grid["emissions"]
        seen: dict[tuple[float, float], float] = {}

        def tonnes(lon: float, lat: float, lats=lats, lons=lons, values=values, seen=seen) -> float:
            if (lon, lat) not in seen:
                i, j = int(round((lat + 89.95) / 0.1)), int(round((lon + 179.95) / 0.1))
                assert abs(lats[i] - lat) < 1e-6 and abs(lons[j] - lon) < 1e-6
                value = float(values[i, j])
                seen[(lon, lat)] = 0.0 if np.isnan(value) else value
            return seen[(lon, lat)]

        def total(place: str, tonnes=tonnes) -> float:
            return sum(tonnes(lon, lat) * share for lon, lat, share in shares[place])

        rwanda = total("__RWANDA__")
        national[(gas, sector, int(year))] = rwanda
        district_sum = 0.0
        for district in sorted(areas):
            t = total(district)
            assert t >= 0
            district_sum += t
            rows.append([district, int(year), gas, sector, SECTORS[sector], round(t, 3),
                         "EDGAR_v8.%s_FT2022_%s" % (ver, kind)])
        if rwanda > 0:
            worst_sum = max(worst_sum, abs(district_sum / rwanda - 1))
        elif district_sum != 0:
            worst_sum = 9.0
    print("bar1 worst districts-vs-Rwanda gap: %.5f%%" % (100 * worst_sum))
    print("bar4 rows:", len(rows), "combos", len(national))
    with open(out, "w", newline="") as fh:
        csv.writer(fh, lineterminator="\n").writerows(
            [["district", "year", "emission_type", "sector", "sector_label", "total_tonnes", "source_version"]] + rows)
    for key in sorted(national):
        if key[2] == 2022:
            print(key, round(national[key], 1))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
