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
