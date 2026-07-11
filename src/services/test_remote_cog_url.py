import pytest

from src.services.remote_cog_url import RemoteCogUrlError, validate_remote_cog_url


@pytest.mark.parametrize(
    "url",
    [
        "https://sentinel-cogs.s3.us-west-2.amazonaws.com/scene/TCI.tif",
        "https://isdasoil.s3.amazonaws.com/soil_data/nitrogen_total.tif",
        "https://sentinel2l2a01.blob.core.windows.net/sentinel2-l2/scene.tif",
        "https://eodata.dataspace.copernicus.eu/scene.tif",
        "https://deafrica-input-datasets.s3.af-south-1.amazonaws.com/scene.tif",
        "mundi-layer:Lorthophoto123",
    ],
)
def test_validate_remote_cog_url_allows_known_eo_hosts(url: str) -> None:
    assert validate_remote_cog_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://sentinel-cogs.s3.us-west-2.amazonaws.com/scene.tif",
        "https://example.com/scene.tif",
        "https://earth-search.aws.element84.com/v1/collections",
        "https://evil.sentinel-cogs.s3.us-west-2.amazonaws.com/scene.tif",
        "https://sentinel-cogs.s3.us-west-2.amazonaws.com.evil.test/scene.tif",
        "https://user@sentinel-cogs.s3.us-west-2.amazonaws.com/scene.tif",
        "https://sentinel-cogs.s3.us-west-2.amazonaws.com:8443/scene.tif",
        "https://sentinel-cogs.s3.us-west-2.amazonaws.com/scene.tif#fragment",
    ],
)
def test_validate_remote_cog_url_rejects_urls_outside_exact_policy(url: str) -> None:
    with pytest.raises(RemoteCogUrlError):
        validate_remote_cog_url(url)


def test_validate_remote_cog_url_accepts_exact_env_additions(monkeypatch) -> None:
    monkeypatch.setenv(
        "REMOTE_COG_ALLOWED_HOSTS",
        "cogs.example.org, imagery.example.net",
    )

    assert (
        validate_remote_cog_url("https://COGS.EXAMPLE.ORG/path/scene.tif")
        == "https://cogs.example.org/path/scene.tif"
    )
    with pytest.raises(RemoteCogUrlError, match="not trusted"):
        validate_remote_cog_url("https://sub.cogs.example.org/path/scene.tif")


def test_validate_remote_cog_url_rejects_non_host_env_entries(monkeypatch) -> None:
    monkeypatch.setenv("REMOTE_COG_ALLOWED_HOSTS", "https://cogs.example.org")

    with pytest.raises(RemoteCogUrlError, match="Invalid REMOTE_COG_ALLOWED_HOSTS"):
        validate_remote_cog_url(
            "https://sentinel-cogs.s3.us-west-2.amazonaws.com/scene.tif"
        )
