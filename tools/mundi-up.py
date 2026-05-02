#!/usr/bin/env python3
"""mundi-up — fast, resumable upload of big GeoTIFFs / orthos to mundi.ai.

Reuses the backend's S3 multipart endpoints (same path the browser uses)
but runs in native Python with proper TCP windows and a local resume state
file. For 2-5 GB drone orthos from a marginal connection, this is the right
tool.

Usage:
    export MUNDI_BASE_URL=https://gis.nozalabs.rw
    export MUNDI_JWT=eyJhbGc...   # paste a fresh Clerk JWT from devtools

    # plain upload
    python mundi-up.py path/to/Cyampirita.tif --project=PYGUU8vW42CU

    # convert to COG first (needs gdal_translate on PATH) — typical 30-50%
    # byte reduction on uncompressed orthos AND skips server COG step.
    python mundi-up.py path/to/Cyampirita.tif --project=PYGUU8vW42CU --cog

If the upload is interrupted, just re-run the same command. Resume picks
up where S3 left off. Resume state lives in ~/.mundi/uploads/<hash>.json.

JWT expires every 60 seconds. If your upload takes longer than that and
the JWT runs out (you'll get HTTP 401), grab a fresh JWT and re-run —
the upload resumes, no work lost.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CHUNK_SIZE = 50 * 1024 * 1024  # match backend MULTIPART_CHUNK_SIZE
CONCURRENCY = 6
PART_RETRIES = 4
STATE_DIR = Path.home() / ".mundi" / "uploads"


def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def env_required(key):
    v = os.environ.get(key)
    if not v:
        die(f"{key} not set in environment")
    return v


def file_fingerprint(path: Path) -> str:
    """SHA-1 of (filename + size + mtime + first 1MB). Stable per-file."""
    h = hashlib.sha1()
    h.update(f"{path.name}|{path.stat().st_size}|{int(path.stat().st_mtime)}".encode())
    with open(path, "rb") as f:
        h.update(f.read(1024 * 1024))
    return h.hexdigest()


def load_state(fp: str):
    p = STATE_DIR / f"{fp}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def save_state(fp: str, state: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"{fp}.json").write_text(json.dumps(state))


def clear_state(fp: str):
    p = STATE_DIR / f"{fp}.json"
    if p.exists():
        p.unlink()


def http_request(method: str, url: str, jwt: str, body=None, headers=None, timeout=60):
    h = {"Authorization": f"Bearer {jwt}"}
    if headers:
        h.update(headers)
    data = None
    if body is not None:
        if isinstance(body, (dict, list)):
            h["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        else:
            data = body
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    return urllib.request.urlopen(req, timeout=timeout)


def get_latest_version_id(base: str, jwt: str, project_id: str) -> str:
    with http_request("GET", f"{base}/api/projects/{project_id}", jwt) as r:
        proj = json.load(r)
    versions = proj.get("maps") or []
    if not versions:
        die(f"project {project_id} has no map versions")
    return versions[-1]


def init_multipart(base: str, jwt: str, version_id: str, filename: str, size: int) -> dict:
    url = f"{base}/api/maps/{version_id}/upload-multipart-init?filename={filename}&file_size={size}"
    with http_request("POST", url, jwt, body=b"") as r:
        return json.load(r)


def status_multipart(base: str, jwt: str, child_map_id: str, upload_id: str, s3_key: str) -> dict:
    from urllib.parse import quote
    url = f"{base}/api/maps/{child_map_id}/upload-multipart-status?upload_id={quote(upload_id)}&s3_key={quote(s3_key)}"
    with http_request("GET", url, jwt) as r:
        return json.load(r)


def presign_part(base: str, jwt: str, child_map_id: str, s3_key: str, upload_id: str, part_num: int) -> str:
    url = f"{base}/api/maps/{child_map_id}/upload-multipart-presign"
    body = {"s3_key": s3_key, "upload_id": upload_id, "part_numbers": [part_num]}
    with http_request("POST", url, jwt, body=body) as r:
        return json.load(r)["urls"][str(part_num)]


def put_part(presigned_url: str, data: bytes) -> str:
    """PUT one part, return its ETag. Returns ETag without quotes."""
    req = urllib.request.Request(presigned_url, data=data, method="PUT")
    with urllib.request.urlopen(req, timeout=300) as r:
        etag = r.headers.get("ETag", "").replace('"', "")
    return etag


def complete_multipart(base: str, jwt: str, child_map_id: str, init: dict, parts: list, original_filename: str) -> dict:
    body = {
        "s3_key": init["s3_key"],
        "upload_id": init["upload_id"],
        "parts": sorted(parts, key=lambda p: p["part_number"]),
        "layer_id": init["layer_id"],
        "filename": original_filename,
        "add_layer_to_map": True,
    }
    url = f"{base}/api/maps/{child_map_id}/upload-multipart-complete"
    with http_request("POST", url, jwt, body=body) as r:
        return json.load(r)


def upload_complete(base: str, jwt: str, child_map_id: str, init: dict, original_filename: str) -> dict:
    body = {
        "s3_key": init["s3_key"],
        "layer_id": init["layer_id"],
        "filename": original_filename,
        "add_layer_to_map": True,
    }
    url = f"{base}/api/maps/{child_map_id}/upload-complete"
    with http_request("POST", url, jwt, body=body, timeout=600) as r:
        return json.load(r)


def make_cog(in_path: Path) -> Path:
    """Run gdal_translate -of COG. Returns new path, or original on failure."""
    if shutil.which("gdal_translate") is None:
        print("gdal_translate not on PATH — skipping COG conversion", file=sys.stderr)
        return in_path
    out_path = in_path.with_name(in_path.stem + ".cog.tif")
    print(f"converting to COG: {in_path.name} → {out_path.name}")
    t0 = time.time()
    try:
        subprocess.check_call([
            "gdal_translate", "-q", "-of", "COG",
            "-co", "COMPRESS=DEFLATE",
            "-co", "PREDICTOR=2",
            "-co", "BIGTIFF=YES",
            "-co", "NUM_THREADS=ALL_CPUS",
            str(in_path), str(out_path),
        ])
    except subprocess.CalledProcessError as e:
        print(f"gdal_translate failed: {e}", file=sys.stderr)
        return in_path
    in_size = in_path.stat().st_size
    out_size = out_path.stat().st_size
    print(f"COG done in {time.time() - t0:.1f}s: {in_size / 1e9:.2f} GB → {out_size / 1e9:.2f} GB ({100 * out_size / in_size:.0f}%)")
    return out_path


def upload_part_with_retry(args):
    base, jwt, child_map_id, s3_key, upload_id, part_num, data = args
    last_err = None
    for attempt in range(PART_RETRIES):
        try:
            url = presign_part(base, jwt, child_map_id, s3_key, upload_id, part_num)
            etag = put_part(url, data)
            return {"part_number": part_num, "etag": etag}
        except Exception as e:
            last_err = e
            if attempt < PART_RETRIES - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"part {part_num} failed after {PART_RETRIES} attempts: {last_err}")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("path", help="Path to .tif (or other geofile) to upload")
    p.add_argument("--project", required=True, help="Project ID (e.g., PYGUU8vW42CU)")
    p.add_argument("--cog", action="store_true", help="Run gdal_translate -of COG before upload")
    p.add_argument("--base-url", default=os.environ.get("MUNDI_BASE_URL", "https://gis.nozalabs.rw"))
    p.add_argument("--jwt", default=os.environ.get("MUNDI_JWT"), help="Clerk JWT (or set MUNDI_JWT)")
    args = p.parse_args()

    if not args.jwt:
        die("set MUNDI_JWT or pass --jwt (paste a fresh Clerk JWT from devtools)")

    src = Path(args.path).expanduser().resolve()
    if not src.exists():
        die(f"file not found: {src}")

    # Optional COG conversion
    upload_path = make_cog(src) if args.cog else src
    upload_filename = upload_path.name
    upload_size = upload_path.stat().st_size

    fp = file_fingerprint(upload_path)
    base = args.base_url.rstrip("/")

    # Resume check
    saved = load_state(fp)
    init = None
    completed_parts = []
    if saved:
        try:
            status = status_multipart(base, args.jwt, saved["dag_child_map_id"], saved["upload_id"], saved["s3_key"])
            if status.get("exists") and status.get("parts"):
                s3_set = {p["part_number"] for p in status["parts"]}
                completed_parts = [p for p in saved["completed_parts"] if p["part_number"] in s3_set]
                pct = 100 * len(completed_parts) // saved["total_parts"]
                ans = input(f"Resume previous upload at ~{pct}% ({len(completed_parts)}/{saved['total_parts']} parts)? [Y/n] ").strip().lower()
                if ans in ("", "y", "yes"):
                    init = {k: saved[k] for k in ("upload_id", "s3_key", "layer_id", "part_size", "total_parts", "dag_child_map_id", "dag_parent_map_id")}
                    print(f"resuming from part {len(completed_parts) + 1}")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        if init is None:
            clear_state(fp)

    if init is None:
        version_id = get_latest_version_id(base, args.jwt, args.project)
        print(f"using project version {version_id}")
        init = init_multipart(base, args.jwt, version_id, upload_filename, upload_size)
        print(f"multipart init: {init['total_parts']} parts × {init['part_size'] // (1024 * 1024)} MB")

    save_state(fp, {**init, "completed_parts": completed_parts, "filename": upload_filename, "file_size": upload_size, "created_at": int(time.time())})

    completed_set = {p["part_number"] for p in completed_parts}
    todo = [pn for pn in range(1, init["total_parts"] + 1) if pn not in completed_set]
    print(f"uploading {len(todo)} parts ({CONCURRENCY} concurrent)")

    t0 = time.time()
    bytes_uploaded = sum(min(init["part_size"], upload_size - (p["part_number"] - 1) * init["part_size"]) for p in completed_parts)

    with open(upload_path, "rb") as f, concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {}
        for pn in todo:
            start = (pn - 1) * init["part_size"]
            end = min(start + init["part_size"], upload_size)
            f.seek(start)
            data = f.read(end - start)
            fut = pool.submit(upload_part_with_retry, (base, args.jwt, init["dag_child_map_id"], init["s3_key"], init["upload_id"], pn, data))
            futures[fut] = (pn, end - start)

        done = len(completed_parts)
        for fut in concurrent.futures.as_completed(futures):
            pn, n = futures[fut]
            try:
                part = fut.result()
            except Exception as e:
                die(f"part {pn} failed: {e}")
            completed_parts.append(part)
            bytes_uploaded += n
            done += 1
            elapsed = time.time() - t0
            mbps = (bytes_uploaded * 8 / 1e6) / max(elapsed, 0.1)
            print(f"  part {done}/{init['total_parts']} done — {100 * bytes_uploaded // upload_size}% — {mbps:.1f} Mbps")
            save_state(fp, {**init, "completed_parts": completed_parts, "filename": upload_filename, "file_size": upload_size, "created_at": int(time.time())})

    print("assembling parts on server…")
    complete_multipart(base, args.jwt, init["dag_child_map_id"], init, completed_parts, upload_filename)

    print("processing on server (creates layer, generates COG, etc)…")
    result = upload_complete(base, args.jwt, init["dag_child_map_id"], init, upload_filename)

    clear_state(fp)
    elapsed = time.time() - t0
    print(f"done in {elapsed:.0f}s. layer: {result.get('name')}  map: {init['dag_child_map_id']}")
    print(f"view: {base}/project/{args.project}/{init['dag_child_map_id']}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted — re-run the same command to resume", file=sys.stderr)
        sys.exit(130)
