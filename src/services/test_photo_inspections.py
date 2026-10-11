"""Photo inspections, the domain: which archives are photo sets, the facts read from a photo (EXIF written by
Pillow as a camera writes it, DJI's XMP packet), storage keys, camera points. Missing values are None, never 0."""

import hashlib
import io
import time

import pytest
from PIL import Image, PngImagePlugin

from src.services import stored_files
from src.services import photo_inspections as pi
from test_fixtures.drone_photos import DJI_XMP, drone_jpg


def _write(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def test_a_dji_photo_gives_every_fact(tmp_path):
    data = drone_jpg(-1.9441, 30.0619, altitude=1532.25, xmp=DJI_XMP, offset="+02:00")
    facts = pi.read_photo(_write(tmp_path, "DJI_0001.JPG", data), "flight/DJI_0001.JPG")

    assert facts.sha256 == hashlib.sha256(data).hexdigest()
    assert facts.filename == "flight/DJI_0001.JPG"
    assert (facts.width, facts.height) == (64, 48)
    assert facts.taken_at == "2026-10-09T14:03:22+02:00"
    assert facts.lat == pytest.approx(-1.9441, abs=1e-6)  # south of the equator
    assert facts.lon == pytest.approx(30.0619, abs=1e-6)
    assert facts.altitude_m == pytest.approx(1532.25)
    assert facts.relative_altitude_m == pytest.approx(45.2)
    assert facts.gimbal_pitch == pytest.approx(-30.1)
    assert facts.gimbal_yaw == pytest.approx(-87.3)
    assert facts.flight_yaw == pytest.approx(-88.1)
    assert (facts.make, facts.model) == ("DJI", "M3E")
    assert facts.has_position and facts.extension == ".jpg"


def test_a_photo_without_metadata_has_none_not_zero(tmp_path):
    data = drone_jpg(make=None, model=None, taken=None)
    facts = pi.read_photo(_write(tmp_path, "plain.jpg", data), "plain.jpg")
    assert (facts.width, facts.height) == (64, 48)
    for name in ("taken_at", "lat", "lon", "altitude_m", "relative_altitude_m", "gimbal_pitch", "gimbal_yaw",
                 "flight_yaw", "make", "model"):
        assert getattr(facts, name) is None, name
    assert not facts.has_position


def test_a_real_zero_is_kept(tmp_path):
    packet = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description>'
              b'<drone-dji:GimbalPitchDegree>0.00</drone-dji:GimbalPitchDegree>'
              b'<drone-dji:RelativeAltitude>+0.00</drone-dji:RelativeAltitude>'
              b'</rdf:Description></rdf:RDF></x:xmpmeta>')
    facts = pi.read_photo(_write(tmp_path, "a.jpg", drone_jpg(1.5, 29.5, altitude=0.0, xmp=packet)), "a.jpg")
    assert facts.altitude_m == 0.0
    assert facts.gimbal_pitch == 0.0  # element form of the packet
    assert facts.relative_altitude_m == 0.0


def test_below_sea_level_and_west_are_negative(tmp_path):
    facts = pi.read_photo(_write(tmp_path, "a.jpg", drone_jpg(31.5, -35.4, altitude=-410.0)), "a.jpg")
    assert facts.lon == pytest.approx(-35.4, abs=1e-6)
    assert facts.altitude_m == pytest.approx(-410.0)


def test_a_void_fix_or_a_missing_hemisphere_is_no_position(tmp_path):
    void = drone_jpg(-1.9, 30.1, gps_extra={9: "V"})
    assert not pi.read_photo(_write(tmp_path, "void.jpg", void), "void.jpg").has_position
    no_ref = drone_jpg(gps_extra={2: (1.0, 56.0, 30.0), 4: (30.0, 3.0, 36.0)})
    assert not pi.read_photo(_write(tmp_path, "noref.jpg", no_ref), "noref.jpg").has_position
    off_globe = drone_jpg(gps_extra={1: "N", 2: (95.0, 0.0, 0.0), 3: "E", 4: (30.0, 0.0, 0.0)})
    assert not pi.read_photo(_write(tmp_path, "off.jpg", off_globe), "off.jpg").has_position


def test_the_xmp_packet_is_found_in_the_file_when_pillow_does_not_report_it(tmp_path):
    # A PNG carrying the packet in a plain text chunk: Pillow files it under "Comment", the scan finds it.
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", DJI_XMP.decode("utf-8"))
    out = io.BytesIO()
    Image.new("RGB", (8, 8)).save(out, "PNG", pnginfo=info)
    facts = pi.read_photo(_write(tmp_path, "a.png", out.getvalue()), "a.png")
    assert facts.relative_altitude_m == pytest.approx(45.2)
    assert facts.flight_yaw == pytest.approx(-88.1)
    assert facts.extension == ".png"


def test_a_file_that_is_not_a_photo_is_refused_by_name(tmp_path):
    with pytest.raises(pi.UnreadablePhoto, match="broken.jpg"):
        pi.read_photo(_write(tmp_path, "broken.jpg", b"not a jpeg at all"), "broken.jpg")
    with pytest.raises(pi.UnreadablePhoto, match="tower.jpg is not a JPEG or PNG photo"):
        pi.read_photo(_write(tmp_path, "tower.jpg", drone_jpg(fmt="PNG")), "tower.jpg")


def test_photo_sets_are_recognised_by_their_files():
    assert pi.is_photo_set(["DJI_0001.JPG", "DJI_0002.jpeg", "flight.MRK"])
    assert pi.is_photo_set(["sub/field.PNG"])
    assert not pi.is_photo_set(["roads.shp", "roads.dbf", "cover.jpg"])  # a zipped shapefile stays a shapefile
    assert not pi.is_photo_set(["notes.txt", "track.kml"])
    assert not pi.is_photo_set(["._DJI_0001.JPG"])  # macOS metadata, not a photo


def test_at_least_one_photo_must_be_placed(tmp_path):
    placed = pi.read_photo(_write(tmp_path, "a.jpg", drone_jpg(-1.9, 30.1)), "a.jpg")
    unplaced = pi.read_photo(_write(tmp_path, "b.jpg", drone_jpg()), "b.jpg")
    pi.check_placeable([placed, unplaced])
    with pytest.raises(pi.NoPhotoPosition, match="None of the 2 photos"):
        pi.check_placeable([unplaced, unplaced])
    with pytest.raises(pi.NoPhotoPosition, match="This photo has no map position"):
        pi.check_placeable([unplaced])


def test_camera_points_and_summary(tmp_path):
    placed = pi.read_photo(_write(tmp_path, "a.jpg", drone_jpg(-1.9, 30.1, xmp=DJI_XMP)), "a.jpg")
    unplaced = pi.read_photo(_write(tmp_path, "b.png", drone_jpg(fmt="PNG")), "b.png")
    prefix = pi.storage_prefix("Pproj", "Llayer")
    assert prefix == "inspections/Pproj/Llayer/"

    points = pi.camera_points([placed, unplaced], prefix)
    assert len(points["features"]) == 1
    feature = points["features"][0]
    assert feature["geometry"] == {"type": "Point", "coordinates": [placed.lon, placed.lat]}
    assert feature["properties"]["photo_key"] == f"inspections/Pproj/Llayer/{placed.sha256}.jpg"
    assert feature["properties"]["gimbal_pitch"] == pytest.approx(-30.1)
    assert pi.summary([placed, unplaced], prefix) == {
        "photos": 2, "with_gps": 1, "without_gps": ["b.png"], "storage_prefix": prefix}


def test_only_this_layers_own_photo_keys_are_found():
    sha = "ab" * 32
    prefix = pi.storage_prefix("Pproj", "Llayer")
    assert pi.stored_prefix({"photo_inspection": {"storage_prefix": prefix}}, "Llayer") == prefix
    assert pi.stored_prefix({"photo_inspection": {"storage_prefix": prefix}}, "Lother") is None
    assert pi.stored_prefix({"photo_inspection": {"storage_prefix": "uploads/u/P/Llayer/"}}, "Llayer") is None
    assert pi.stored_prefix({}, "Llayer") is None

    keys = [f"{prefix}{sha}.txt", f"inspections/Pproj/Lother/{sha}.jpg", f"{prefix}{sha}.jpg"]
    assert pi.photo_key_in(prefix, sha, keys) == f"{prefix}{sha}.jpg"
    assert pi.photo_key_in(prefix, sha, keys[:2]) is None
    assert pi.content_type(f"{prefix}{sha}.jpg") == "image/jpeg"
    assert pi.is_sha256(sha) and not pi.is_sha256(sha.upper()) and not pi.is_sha256("../" + sha[3:])


def test_the_switch_is_off_unless_set(monkeypatch):
    monkeypatch.delenv("PHOTO_INSPECTIONS", raising=False)
    assert not pi.enabled()
    monkeypatch.setenv("PHOTO_INSPECTIONS", "1")
    assert pi.enabled()
    monkeypatch.setenv("PHOTO_INSPECTIONS", "0")
    assert not pi.enabled()


def test_the_limits():
    assert pi.MAX_PHOTOS == 1_000
    assert pi.MAX_UNPACKED_BYTES == 15 * 1024 ** 3


def _png_with_xmp(packet: str) -> bytes:
    """A small PNG whose XMP packet is a compressed iTXt chunk, as Adobe tools write it (Pillow reports it under
    "XML:com.adobe.xmp")."""
    info = PngImagePlugin.PngInfo()
    info.add_itxt("XML:com.adobe.xmp", packet, zip=True)
    out = io.BytesIO()
    Image.new("RGB", (8, 8)).save(out, "PNG", pnginfo=info)
    return out.getvalue()


@pytest.mark.parametrize("name,data", [
    # A 1,000,000-letter packet in a PNG of about 1 KB: the old pattern needed hours per value, holding the GIL.
    ("letters.png", lambda: _png_with_xmp("a" * 1_000_000)),
    # The same in a JPEG (an APP1 segment holds at most 64 KB): the old pattern took about 40 s for the photo.
    ("letters.jpg", lambda: drone_jpg(xmp=b"a" * 60_000)),
    # A packet that only the scan of the file head finds (a plain text chunk), around a long run of letters.
    ("scanned.png", lambda: _png_with_comment("<x:xmpmeta " + "a" * 900_000 + "</x:xmpmeta>")),
    # Many prefixes and spaces before names that never get a value.
    ("prefixes.png", lambda: _png_with_xmp(("a:RelativeAltitude" + " " * 40 + "<b:GimbalYawDegree ") * 12_000)),
])
def test_a_crafted_xmp_packet_is_read_in_linear_time(tmp_path, name, data):
    path = _write(tmp_path, name, data())
    started = time.perf_counter()
    facts = pi.read_photo(path, name)
    assert time.perf_counter() - started < 1.0
    for field in ("relative_altitude_m", "gimbal_pitch", "gimbal_yaw", "flight_yaw"):
        assert getattr(facts, field) is None, field


def test_only_the_start_of_a_long_packet_is_searched(tmp_path):
    early = _png_with_xmp('<x:xmpmeta><rdf:Description drone-dji:RelativeAltitude="+45.20" ' + "a" * 300_000
                          + ' drone-dji:GimbalYawDegree="-87.30"/></x:xmpmeta>')
    facts = pi.read_photo(_write(tmp_path, "early.png", early), "early.png")
    assert facts.relative_altitude_m == pytest.approx(45.2)
    assert facts.gimbal_yaw is None, "a value past the first 256 KiB is not read, and missing is None"


def _png_with_comment(text: str) -> bytes:
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", text)
    out = io.BytesIO()
    Image.new("RGB", (8, 8)).save(out, "PNG", pnginfo=info)
    return out.getvalue()


def test_the_photo_is_hashed_by_the_one_file_hash_owner(tmp_path, monkeypatch):
    path = _write(tmp_path, "a.jpg", drone_jpg(-1.9, 30.1))
    calls = []
    real = stored_files.file_sha256
    monkeypatch.setattr(stored_files, "file_sha256", lambda p: calls.append(p) or real(p))
    assert pi.read_photo(path, "a.jpg").sha256 == hashlib.sha256(open(path, "rb").read()).hexdigest()
    assert calls == [path]


class _Listing:
    """Object storage answering list_objects_v2 / delete_object as S3 does (Contents, IsTruncated,
    NextContinuationToken; at most `page` keys a page)."""

    def __init__(self, keys, page=2):
        self.keys, self.page, self.deleted = sorted(keys), page, []

    async def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        matching = [k for k in self.keys if k.startswith(Prefix)]
        start = int(ContinuationToken or 0)
        chunk = matching[start:start + self.page]
        answer = {"KeyCount": len(chunk), "IsTruncated": start + self.page < len(matching)}
        if chunk:
            answer["Contents"] = [{"Key": k, "Size": 1} for k in chunk]
        if answer["IsTruncated"]:
            answer["NextContinuationToken"] = str(start + self.page)
        return answer

    async def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        return {}


class _Rows:
    def __init__(self, count):
        self.count = count

    async def fetchval(self, query, *args):
        assert "map_layers" in query and args == ("Llayer",)
        return self.count


@pytest.mark.anyio
async def test_forget_deletes_every_page_of_the_layers_own_prefix_only_without_its_row():
    prefix = pi.storage_prefix("Pproj", "Llayer")
    own = [f"{prefix}{c * 64}.jpg" for c in "abcde"]
    others = ["inspections/Pproj/Llayer2/" + "f" * 64 + ".jpg", "uploads/u/Pproj/Llayer.zip"]
    store = _Listing(own + others)

    assert await pi.forget_if_unused(_Rows(1), store, "b", prefix, "Llayer") == []  # the layer exists: kept
    assert store.deleted == []
    assert sorted(await pi.forget_if_unused(_Rows(0), store, "b", prefix, "Llayer")) == own  # 3 pages of 2
    assert sorted(store.deleted) == own


@pytest.mark.anyio
@pytest.mark.parametrize("prefix,layer_id", [
    ("", "Llayer"), ("inspections/", "Llayer"), ("uploads/u/P/Llayer/", "Llayer"),
    ("inspections/P/Lother/", "Llayer"), ("inspections/P//", ""), ("inspections/P/Llayer", "Llayer"),
])
async def test_forget_refuses_a_prefix_that_is_not_the_layers_own(prefix, layer_id):
    store = _Listing([])
    with pytest.raises(ValueError):
        await pi.forget_if_unused(_Rows(0), store, "b", prefix, layer_id)
    assert store.deleted == []


# --- Facts the tower work reads: the photo upright, which way it looked, all photos kept ------------------------------

def _rotated_jpg(orientation: int) -> bytes:
    """A 64 x 48 photo whose EXIF says to turn it (6: a quarter clockwise, as phones write portrait photos)."""
    image = Image.new("RGB", (64, 48), (90, 140, 60))
    exif = Image.Exif()
    exif[274] = orientation
    out = io.BytesIO()
    image.save(out, "JPEG", exif=exif)
    return out.getvalue()


def test_a_photo_is_sized_as_it_is_shown(tmp_path):
    turned = pi.read_photo(_write(tmp_path, "p.jpg", _rotated_jpg(6)), "p.jpg")
    assert (turned.width, turned.height, turned.orientation) == (64, 48, 6)  # the stored pixels
    assert turned.upright_size == (48, 64)  # as a browser and the finder show it
    level = pi.read_photo(_write(tmp_path, "q.jpg", _rotated_jpg(1)), "q.jpg")
    assert level.upright_size == (64, 48)
    plain = pi.read_photo(_write(tmp_path, "r.jpg", drone_jpg()), "r.jpg")
    assert plain.orientation is None and plain.upright_size == (64, 48)


@pytest.mark.parametrize("pitch, view", [(-90.0, "nadir"), (-80.0, "nadir"), (-79.9, "oblique"), (-30.1, "oblique"),
                                         (0.0, "oblique"), (15.0, "oblique"), (None, None)])
def test_which_way_a_photo_looked_comes_from_its_gimbal_pitch_only(tmp_path, pitch, view):
    xmp = None if pitch is None else (
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description drone-dji:GimbalPitchDegree="'
        + f"{pitch:+.2f}".encode() + b'"/></rdf:RDF></x:xmpmeta>')
    facts = pi.read_photo(_write(tmp_path, "v.jpg", drone_jpg(-1.9, 30.1, xmp=xmp)), "v.jpg")
    assert facts.gimbal_pitch == (None if pitch is None else pytest.approx(pitch))
    assert pi.view_of(facts) == view  # no pitch: not known, never guessed


class _Store:
    """put_object / get_object as aiobotocore answers them (a body read through an async context manager)."""

    class exceptions:
        class NoSuchKey(Exception):
            pass

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    async def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body
        return {}

    async def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey(Key)
        data = self.objects[Key]

        class Body:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def read(self):
                return data

        return {"Body": Body()}


@pytest.mark.anyio
async def test_every_photos_facts_are_kept_beside_the_originals(tmp_path):
    placed = pi.read_photo(_write(tmp_path, "a.jpg", drone_jpg(-1.9, 30.1, xmp=DJI_XMP)), "a.jpg")
    unplaced = pi.read_photo(_write(tmp_path, "b.jpg", _rotated_jpg(6)), "b.jpg")
    prefix = pi.storage_prefix("Pproj", "Llayer")
    store = _Store()
    assert await pi.load_facts(store, "b", prefix) is None  # an inspection uploaded before facts were kept
    await pi.save_facts(store, "b", prefix, [placed, unplaced])
    assert list(store.objects) == [f"{prefix}facts.json"] == [pi.facts_key(prefix)]
    assert await pi.load_facts(store, "b", prefix) == [placed, unplaced]
    assert pi.photo_key_in(prefix, "facts", list(store.objects)) is None  # never taken for a photo
