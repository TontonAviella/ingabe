import pytest
from botocore.exceptions import ClientError

from src.utils import get_async_s3_client, get_bucket_name


@pytest.mark.s3
@pytest.mark.anyio
async def test_s3_get_nonexistent_key_raises_client_error():
    """Verify S3 client raises ClientError for non-existent keys (baseline behavior)."""
    s3 = await get_async_s3_client()
    bucket = get_bucket_name()

    with pytest.raises(ClientError) as exc_info:
        await s3.get_object(Bucket=bucket, Key="nonexistent/key/that/does/not/exist.txt")

    error_code = exc_info.value.response["Error"]["Code"]
    # MinIO returns "NoSuchKey", AWS S3 can return "404"
    assert error_code in ("NoSuchKey", "404")


@pytest.mark.s3
@pytest.mark.anyio
async def test_s3_head_nonexistent_key_raises_client_error():
    """Verify S3 head_object raises ClientError for missing keys."""
    s3 = await get_async_s3_client()
    bucket = get_bucket_name()

    with pytest.raises(ClientError) as exc_info:
        await s3.head_object(Bucket=bucket, Key="nonexistent/basemap-preview.png")

    error_code = exc_info.value.response["Error"]["Code"]
    # Handle both MinIO and AWS S3 error codes
    assert error_code in ("NoSuchKey", "404", "Not Found")


@pytest.mark.anyio
async def test_basemap_render_survives_s3_cache_miss(auth_client):
    """Basemap render endpoint should work even when S3 cache is empty.

    The /api/basemaps/render.png endpoint tries S3 cache first via head_object,
    then falls through to render if cache miss. Should not return 500.
    """
    response = await auth_client.get("/api/basemaps/render.png?basemap=openstreetmap")

    # Should either:
    # 1. Return 200 with rendered image
    # 2. Return 400 if basemap name invalid
    # 3. Return 503/502 if renderer unavailable
    # But should NOT be a 500 internal server error
    assert response.status_code != 500, f"Got unexpected 500: {response.text}"

    # Most likely outcome: successful render
    if response.status_code == 200:
        assert response.headers["content-type"] == "image/png"
        assert len(response.content) > 0


@pytest.mark.anyio
async def test_basemap_render_handles_invalid_basemap_name(auth_client):
    """Basemap render with invalid name should return 400, not crash."""
    response = await auth_client.get("/api/basemaps/render.png?basemap=nonexistent_basemap_xyz")

    # Should return 400 Bad Request for invalid basemap name
    assert response.status_code == 400
    body = response.json()
    assert "detail" in body
    assert "invalid basemap" in body["detail"].lower()


def test_s3_bucket_name_configured():
    """Verify S3 bucket name is configured and non-empty."""
    bucket = get_bucket_name()
    assert bucket
    assert isinstance(bucket, str)
    assert len(bucket) > 0
