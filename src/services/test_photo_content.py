import asyncio

from src.services import photo_content
from src.services.test_farm_records import _FakeS3


class _S3WithHead(_FakeS3):
    async def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey()
        return {}


def test_same_bytes_hash_the_same_whatever_the_name(tmp_path):
    a, b, c = tmp_path / "farm.tif", tmp_path / "renamed copy.tif", tmp_path / "other.tif"
    a.write_bytes(b"orthophoto" * 1000)
    b.write_bytes(b"orthophoto" * 1000)
    c.write_bytes(b"orthophoto" * 999 + b"x")
    assert photo_content.file_sha256(str(a)) == photo_content.file_sha256(str(b)) != photo_content.file_sha256(str(c))


def test_index_keys_differ_by_target_crs():
    assert photo_content.index_key("ab", "EPSG:3857") == "photo_content/v1/ab-epsg_3857.json"
    assert photo_content.index_key("ab", "") != photo_content.index_key("ab", "EPSG:3857")


def test_remembered_photo_is_found_only_while_it_is_stored():
    s3 = _S3WithHead()

    async def run():
        assert await photo_content.find(s3, "b", "sha", "EPSG:3857") is None
        s3.objects["cog/layer/L1.cog.tif"] = b"cog"
        await photo_content.remember(s3, "b", "sha", "EPSG:3857", "cog/layer/L1.cog.tif", "EPSG:3857")
        assert await photo_content.find(s3, "b", "sha", "EPSG:3857") == {
            "cog_key": "cog/layer/L1.cog.tif", "cog_srs": "EPSG:3857"}
        assert await photo_content.find(s3, "b", "sha", "EPSG:4326") is None  # other target CRS: other photo
        del s3.objects["cog/layer/L1.cog.tif"]
        assert await photo_content.find(s3, "b", "sha", "EPSG:3857") is None  # photo gone: optimise again

    asyncio.run(run())


class _S3WithDelete(_S3WithHead):
    async def list_objects_v2(self, Bucket, Prefix):
        return {"Contents": [{"Key": k} for k in self.objects if k.startswith(Prefix)]}

    async def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


class _Layers:
    """map_layers as seen by a worker connection: how many layers still point at a photo."""

    def __init__(self, using: int):
        self.using = using

    async def fetchval(self, sql, cog_key):
        return self.using


def test_a_photo_and_its_analyses_go_with_its_last_layer():
    """Copies share one optimised photo: it, its index entry and its found plots stay while any layer uses it and
    are deleted with the last one; another photo's index entry for the same bytes is left alone (audit R1-23)."""
    from src.services import drone_plots

    cog = "cog/layer/L1.cog.tif"
    meta = {"cog_key": cog, "content_sha256": "sha"}

    async def run():
        s3 = _S3WithDelete()
        s3.objects[cog] = b"cog"
        s3.objects[drone_plots.stored_plots_key(cog)] = b"{}"
        await photo_content.remember(s3, "b", "sha", "EPSG:3857", cog, "EPSG:3857")
        await photo_content.remember(s3, "b", "sha", "EPSG:4326", "cog/layer/L2.cog.tif", "EPSG:4326")

        assert await photo_content.forget_if_unused(_Layers(using=1), s3, "b", meta) == []  # a copy still uses it
        assert cog in s3.objects

        deleted = await photo_content.forget_if_unused(_Layers(using=0), s3, "b", meta)
        assert set(deleted) == {cog, drone_plots.stored_plots_key(cog), photo_content.index_key("sha", "EPSG:3857")}
        assert photo_content.index_key("sha", "EPSG:4326") in s3.objects  # the other CRS's photo is not this one
        assert await photo_content.forget_if_unused(_Layers(using=0), s3, "b", {}) == []  # no photo: nothing to do

    asyncio.run(run())
