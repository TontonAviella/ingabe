"""The reader says which band is which, for a photo whose file does not.

Most processing programs write no band names into a multispectral file (Agisoft Metashape, DJI Terra), and the
order differs from camera to camera, so such a file is unknown to the app and nothing is measured from it
(photo_bands). Only a person who knows the camera can say. What they say is written into the app's own copy of the
photo as band descriptions, with a note that a person said it, and from then on every reader of the photo sees
named bands. Nothing in the pixels changes. A file that names its own bands cannot be renamed here: the file's own
word stands.

For a thermal band the reader may also say the unit, since degrees are said only when the unit is known.
"""

from __future__ import annotations

import os
import subprocess
import xml.etree.ElementTree as ET
from typing import Any, Optional

from src.services import photo_bands

ROLES = ("blue", "green", "red", "red_edge", "nir", "thermal", "other")
SAID = {"blue": "Blue", "green": "Green", "red": "Red", "red_edge": "Red edge", "nir": "Near-infrared",
        "thermal": "Thermal", "other": "Another band"}
DESCRIPTION = {"blue": "Blue", "green": "Green", "red": "Red", "red_edge": "RedEdge", "nir": "NIR",
               "thermal": "Thermal", "other": "Other"}
UNIT = {photo_bands.CELSIUS: "C", photo_bands.KELVIN: "K"}
SAID_BY_TAG = "INGABE_BANDS_SAID_BY"  # on the app's copy: the band names came from a person, not from the file

# The usual orders, offered by band count so the reader taps one instead of filling a form. The camera's maker
# fixes the order; the processing program keeps it.
PRESETS: dict[int, list[dict[str, Any]]] = {
    1: [{"label": "A thermal photo", "order": ["thermal"]}],
    4: [{"label": "Green, Red, Red edge, Near-infrared (DJI Mavic 3 Multispectral, Parrot Sequoia)",
         "order": ["green", "red", "red_edge", "nir"]},
        {"label": "Blue, Green, Red, Near-infrared", "order": ["blue", "green", "red", "nir"]}],
    5: [{"label": "Blue, Green, Red, Red edge, Near-infrared (MicaSense RedEdge, DJI Phantom 4 Multispectral)",
         "order": ["blue", "green", "red", "red_edge", "nir"]},
        {"label": "Blue, Green, Red, Near-infrared, Red edge", "order": ["blue", "green", "red", "nir", "red_edge"]}],
    6: [{"label": "Blue, Green, Red, Red edge, Near-infrared, Thermal (MicaSense Altum)",
         "order": ["blue", "green", "red", "red_edge", "nir", "thermal"]},
        {"label": "Blue, Green, Red, Near-infrared, Red edge, Thermal",
         "order": ["blue", "green", "red", "nir", "red_edge", "thermal"]}],
}


def check(order: list[str], band_count: int, thermal_unit: Optional[str] = None) -> None:
    """Raise ValueError, in words a reader can act on, unless `order` names each band of the photo once."""
    if len(order) != band_count:
        raise ValueError(f"This photo has {band_count} band{'s' if band_count != 1 else ''}: name each one.")
    unknown = [role for role in order if role not in ROLES]
    if unknown:
        raise ValueError(f"\"{unknown[0]}\" is not a band the app knows.")
    named = [role for role in order if role != "other"]
    twice = sorted({SAID[role] for role in named if named.count(role) > 1})
    if twice:
        raise ValueError(f"{twice[0]} is given to two bands: each band is one thing.")
    if "thermal" not in named and not ("red" in named and "nir" in named):
        raise ValueError("Name at least the red and the near-infrared band, or a thermal band: nothing can be "
                         "measured without them.")
    if thermal_unit is not None and (thermal_unit not in UNIT or "thermal" not in named):
        raise ValueError("The unit is for a thermal band: degrees Celsius or kelvin.")


def rewrite(source: str, target: str, order: list[str], thermal_unit: Optional[str], folder: str) -> None:
    """Write `source` again as `target` with its bands described as `order` says (blocking). The pixels are
    copied as they are; only what the file says about its bands changes."""
    vrt = os.path.join(folder, "named.vrt")
    try:
        subprocess.run(["gdalbuildvrt", "-q", vrt, source], check=True, capture_output=True, timeout=300)
        tree = ET.parse(vrt)
        root = tree.getroot()
        bands = root.findall("VRTRasterBand")
        if len(bands) != len(order):
            raise ValueError("the photo's bands could not be read to name them")
        for band, role in zip(bands, order):
            described = band.find("Description")
            (described if described is not None else ET.SubElement(band, "Description")).text = DESCRIPTION[role]
            if role == "thermal" and thermal_unit:
                unit = band.find("UnitType")
                (unit if unit is not None else ET.SubElement(band, "UnitType")).text = UNIT[thermal_unit]
        meta = root.find("Metadata")
        if meta is None:
            meta = ET.SubElement(root, "Metadata")
        ET.SubElement(meta, "MDI", {"key": SAID_BY_TAG}).text = "reader"
        tree.write(vrt)
        subprocess.run(["gdal_translate", "-q", "-of", "COG", "-co", "BIGTIFF=YES", "-co", "BLOCKSIZE=512",
                        "-co", "COMPRESS=DEFLATE", "-co", "OVERVIEWS=AUTO", "-co", "OVERVIEW_RESAMPLING=NEAREST",
                        vrt, target], check=True, capture_output=True, timeout=3600)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise OSError("the photo could not be written again with its bands named") from None


def said_by_reader(ds: Any) -> bool:
    """Whether this copy's band names came from a person (band_names.rewrite), not from the file as uploaded."""
    return ds.tags().get(SAID_BY_TAG) == "reader"


def state(ds: Any) -> dict[str, Any]:
    """What the photo says of its bands, and whether the reader is needed (blocking: reads the file's header)."""
    bands = photo_bands.of(ds)
    count = int(ds.count)
    roles = {role: getattr(bands, role) for role in ("blue", "green", "red", "red_edge", "nir", "thermal")
             if getattr(bands, role)}
    if count == 1 and (photo_bands.described_as(ds.descriptions[0]) == "thermal"
                       or photo_bands.thermal_unit(ds.units[0], ds.descriptions[0])):
        roles = {"thermal": 1}  # a one-band photo that says it is thermal
    unnamed = bands.kind == photo_bands.UNKNOWN and not roles
    return {"band_count": count, "kind": bands.kind, "roles": roles,
            "needs_names": unnamed,
            "said_by_reader": said_by_reader(ds),
            "presets": PRESETS.get(count, []) if unnamed else [],
            "choices": [{"id": role, "label": SAID[role]} for role in ROLES]}
