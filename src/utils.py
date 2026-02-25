import os
import logging
import boto3
import tempfile
import zipfile
import shutil
import aioboto3
import asyncio
import secrets

logger = logging.getLogger(__name__)
from functools import lru_cache
from openai import AsyncOpenAI
from fastapi import Request


def generate_id(length=12, prefix=""):
    """Generate a unique ID for the map or layer.

    Using characters [1-9A-HJ-NP-Za-km-z] (excluding 0, O, I, l)
    to avoid ambiguity in IDs.
    """
    assert len(prefix) in [0, 1], "Prefix must be at most 1 character"
    valid_chars = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    result = "".join(secrets.choice(valid_chars) for _ in range(length - len(prefix)))
    return prefix + result


@lru_cache
def get_s3_client():
    config = boto3.session.Config(
        signature_version="s3",
    )
    return boto3.Session().client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT_URL"],
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
        region_name=os.environ["S3_DEFAULT_REGION"],
        config=config,
    )


# shared session, cache client per asyncio loop and signature_version
_session = aioboto3.Session()
_clients = {}


async def get_async_s3_client(signature_version: str = "s3v4"):
    loop = asyncio.get_running_loop()
    key = (loop, signature_version)
    if key not in _clients:
        config = boto3.session.Config(
            signature_version=signature_version,
        )
        _clients[key] = await _session.client(
            "s3",
            endpoint_url=os.environ["S3_ENDPOINT_URL"],
            aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
            region_name=os.environ["S3_DEFAULT_REGION"],
            config=config,
        ).__aenter__()
    return _clients[key]


def get_bucket_name():
    return os.environ["S3_BUCKET"]


async def process_zip_with_shapefile(zip_file_path):
    temp_dir = tempfile.mkdtemp()

    try:
        with zipfile.ZipFile(zip_file_path, "r") as zip_ref:
            zip_ref.extractall(temp_dir)

        # Find all .shp files in the extracted directory, excluding __MACOSX folders
        shp_files = []
        for root, _, files in os.walk(temp_dir):
            # Skip __MACOSX directories
            if "__MACOSX" in root:
                continue

            for file in files:
                if file.lower().endswith(".shp"):
                    shp_files.append(os.path.join(root, file))

        if not shp_files:
            raise ValueError("No shapefile found in the ZIP archive")

        if len(shp_files) > 1:
            raise ValueError(
                "Multiple shapefiles found in the ZIP archive. Only one shapefile is supported."
            )

        gpkg_file_path = os.path.join(temp_dir, "converted.gpkg")
        shp_file = shp_files[0]

        layer_name = os.path.splitext(os.path.basename(shp_file))[0]

        ogr_cmd = [
            "ogr2ogr",
            "-f",
            "GPKG",
            gpkg_file_path,
            shp_file,
            "-nln",
            layer_name,
        ]

        process = await asyncio.create_subprocess_exec(*ogr_cmd)
        await process.wait()

        if process.returncode != 0:
            raise Exception(
                f"ogr2ogr command failed with exit code {process.returncode}"
            )

        return gpkg_file_path, temp_dir

    except Exception as e:
        logger.error("ZIP/Shapefile conversion failed: %s", e)
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def process_kmz_to_kml(kmz_file_path):
    temp_dir = tempfile.mkdtemp()

    try:
        with zipfile.ZipFile(kmz_file_path, "r") as zip_ref:
            zip_ref.extractall(temp_dir)

        # Find the first .kml file in the extracted directory
        kml_file = None
        for root, _, files in os.walk(temp_dir):
            for file in files:
                if file.lower().endswith(".kml"):
                    kml_file = os.path.join(root, file)
                    break
            if kml_file:
                break

        if not kml_file:
            raise ValueError("No KML file found in the KMZ archive")

        return kml_file, temp_dir

    except Exception as e:
        logger.error("KMZ to KML conversion failed: %s", e)
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


async def s3_op(coro, operation: str, resource_id: str = "", *, raise_http: bool = True):
    """Execute an S3 coroutine with standardised error handling.

    Args:
        coro: The awaitable S3 operation (e.g. ``s3.download_file(...)``).
        operation: Human-readable verb such as ``"download"`` or ``"upload"``.
        resource_id: Optional identifier for log context (e.g. ``"layer abc123"``).
        raise_http: If *True* raise :class:`~fastapi.HTTPException` with 502;
            otherwise raise :class:`RuntimeError`.

    Returns:
        Whatever the underlying coroutine returns.
    """
    from botocore.exceptions import ClientError
    from fastapi import HTTPException, status as http_status

    try:
        return await coro
    except ClientError as exc:
        ctx = f" for {resource_id}" if resource_id else ""
        logger.error("S3 %s failed%s: %s", operation, ctx, exc)
        if raise_http:
            raise HTTPException(
                status_code=http_status.HTTP_502_BAD_GATEWAY,
                detail=f"Storage error ({operation}{ctx}): {exc}",
            )
        raise RuntimeError(f"Storage service temporarily unavailable: {exc}") from exc


def get_openai_client(request: Request) -> AsyncOpenAI:
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    return AsyncOpenAI(base_url=base_url)
