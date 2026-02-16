import os
import pytest
from src.symbology.verify import verify_full_style_json_str, StyleValidationError


@pytest.mark.anyio
async def test_verify_valid_style_json():
    with open(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "test_fixtures",
            "maplibre_valid_style.json",
        ),
        "r",
    ) as f:
        style_json_str = f.read()

    is_valid = await verify_full_style_json_str(style_json_str)
    assert is_valid, "Valid style was incorrectly marked as invalid"


@pytest.mark.anyio
async def test_verify_invalid_style_json():
    with open(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "test_fixtures",
            "maplibre_invalid_style.json",
        ),
        "r",
    ) as f:
        style_json_str = f.read()

    try:
        result = await verify_full_style_json_str(style_json_str)
        assert not result, "Invalid style was incorrectly marked as valid"
    except StyleValidationError as e:
        assert 'source "crimea" not found' in str(e), (
            "Expected error message not found in exception"
        )
