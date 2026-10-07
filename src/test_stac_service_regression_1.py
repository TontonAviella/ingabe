"""Regression coverage for STAC discovery across the offered catalogs."""

from unittest.mock import Mock

import pytest
import requests

import src.services.stac_service as stac_module
from src.services.stac_service import SENTINEL2_COLLECTIONS, STAC_CATALOGS, STACService


# Dropped on 2026-10-07 because NDVI could never read their band files: CDSE's need CDSE credentials
# (ISSUE-001 of 2026-07-09 had fixed its endpoint), Planetary Computer's a signed URL. Re-adding one
# needs that access in the band readers first.
@pytest.mark.parametrize("catalog", ["cdse", "planetary_computer"])
def test_catalogs_whose_bands_we_cannot_read_are_not_offered(catalog) -> None:
    assert catalog not in STAC_CATALOGS
    assert catalog not in SENTINEL2_COLLECTIONS


@pytest.mark.parametrize("provider", ["earth_search"])
def test_each_stac_provider_posts_and_normalizes_results(provider, monkeypatch) -> None:
    monkeypatch.setattr(stac_module, "_PYSTAC_CLIENT_AVAILABLE", False)
    service = STACService(provider)
    response = Mock()
    response.json.return_value = {
        "features": [
            {
                "id": f"{provider}-scene",
                "bbox": [29.0, -2.0, 30.0, -1.0],
                "properties": {
                    "datetime": "2026-07-10T08:00:00Z",
                    "eo:cloud_cover": 4.5,
                    "platform": "sentinel-2b",
                },
                "assets": {
                    "visual": {
                        "href": "https://sentinel-cogs.s3.us-west-2.amazonaws.com/scene.tif",
                        "type": "image/tiff",
                    },
                    "ignored": {"href": "https://example.com/ignored"},
                },
            }
        ]
    }
    monkeypatch.setattr(service._session, "post", Mock(return_value=response))

    result = service.search_imagery(
        bbox=[29.0, -2.0, 30.0, -1.0],
        datetime_range="2026-07-01/2026-07-10",
        max_cloud_cover=10,
        limit=3,
    )

    service._session.post.assert_called_once()
    call = service._session.post.call_args
    assert call.args[0] == f"{STAC_CATALOGS[provider]}/search"
    assert call.kwargs["json"]["collections"] == [SENTINEL2_COLLECTIONS[provider]]
    assert result["catalog"] == provider
    assert result["items"] == [
        {
            "id": f"{provider}-scene",
            "datetime": "2026-07-10T08:00:00Z",
            "cloud_cover": 4.5,
            "platform": "sentinel-2b",
            "bbox": [29.0, -2.0, 30.0, -1.0],
            "assets": {
                "visual": {
                    "href": "https://sentinel-cogs.s3.us-west-2.amazonaws.com/scene.tif",
                    "type": "image/tiff",
                }
            },
        }
    ]


@pytest.mark.parametrize("provider", ["earth_search"])
def test_each_stac_provider_returns_scoped_errors(provider, monkeypatch) -> None:
    monkeypatch.setattr(stac_module, "_PYSTAC_CLIENT_AVAILABLE", False)
    service = STACService(provider)
    monkeypatch.setattr(
        service._session,
        "post",
        Mock(side_effect=requests.ConnectionError(f"{provider} unavailable")),
    )

    result = service.search_imagery()

    assert result["catalog"] == provider
    assert f"{provider} unavailable" in result["error"]
