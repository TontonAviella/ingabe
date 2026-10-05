"""Rwanda admin-boundary features for the boundary seed migrations.

One home for where the seed migrations (e1f2a3b4c5d6, f2a3b4c5d6e7,
b2c3d4e5f6a7) get geoBoundaries gbOpen RWA features from. Levels: ADM2
districts, ADM3 sectors, ADM4 cells, ADM5 villages (simplified geometry).

The features come from files vendored under seed_data/geoboundaries/, never
from the network: a migration that downloads from a third-party site fails
whenever that site is slow or down (CI, 2026-10-04: ConnectTimeout on the
geoboundaries.org API). Source, licence and attribution: that directory's
README.md.
"""

import hashlib
import json
import lzma
from pathlib import Path

_SEED_DIR = Path(__file__).parent / "seed_data" / "geoboundaries"

# level -> (vendored file, SHA-256 of the decompressed GeoJSON). The hashes
# are those of the files at geoBoundaries commit 9469f09; ADM5 is the
# simplified file because the full one is ~130 MB.
_SOURCES = {
    "ADM2": (
        "geoBoundaries-RWA-ADM2.geojson.xz",
        "efe97dd43536b7de34a9b07bf0408a766f2020ac54765b1e1f9da3631b941299",
    ),
    "ADM3": (
        "geoBoundaries-RWA-ADM3.geojson.xz",
        "6e2c064eea41093c33c886b3c29cf403dfe29da10197bc9f2df2155884cf66b6",
    ),
    "ADM4": (
        "geoBoundaries-RWA-ADM4.geojson.xz",
        "9c8e21140c557463af2e7b1683634154a403e2bc351d48fd3935b24b5a84e95e",
    ),
    "ADM5": (
        "geoBoundaries-RWA-ADM5_simplified.geojson.xz",
        "d02f5e5de653fed788efa03292ca861675fecf1ae0657cd5c8caa7970e72172c",
    ),
}


def rwanda_boundary_features(level: str) -> list[dict]:
    """Return the GeoJSON features for one Rwanda admin level.

    Raises if the vendored file is missing or its content does not match the
    pinned hash; callers decide whether that is fatal.
    """
    filename, expected_sha256 = _SOURCES[level]
    raw = lzma.decompress((_SEED_DIR / filename).read_bytes())
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        raise RuntimeError(
            f"{filename}: SHA-256 {actual_sha256} does not match the pinned "
            f"{expected_sha256}"
        )
    return json.loads(raw)["features"]
