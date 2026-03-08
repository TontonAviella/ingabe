#!/usr/bin/env python3
"""Verify Sentinel Hub credentials and test tile fetching.

The Process API approach doesn't require instance/layer configuration —
evalscripts are sent directly per request. This script validates that
credentials work and the API is accessible.

For PlanetScope: data must be imported via BYOC/TPDI before tiles are available.
Sentinel-2 L2A is immediately available worldwide.

Usage:
    export SH_CLIENT_ID=...
    export SH_CLIENT_SECRET=...
    python3 scripts/setup_sentinel_hub.py

Account: ntabukiraniroroger@gmail.com
Org:     WS_c3fbbb6d-1408-47e5-ab01-d7641d3d3ead
"""

import argparse
import os
import sys

import requests

SH_TOKEN_URL = (
    "https://services.sentinel-hub.com/auth/realms/main/"
    "protocol/openid-connect/token"
)
SH_PROCESS_URL = "https://services.sentinel-hub.com/api/v1/process"


def get_token(client_id: str, client_secret: str) -> str:
    resp = requests.post(
        SH_TOKEN_URL,
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def test_s2_tile(token: str) -> bool:
    """Test fetching a Sentinel-2 tile for central Rwanda."""
    payload = {
        "input": {
            "bounds": {
                "bbox": [3101508.86, -694659.71, 3111292.80, -684875.77],
                "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/3857"},
            },
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {
                    "timeRange": {"from": "2025-12-01T00:00:00Z", "to": "2026-03-08T23:59:59Z"},
                    "maxCloudCoverage": 30,
                    "mosaickingOrder": "mostRecent",
                },
            }],
        },
        "output": {
            "width": 256,
            "height": 256,
            "responses": [{"identifier": "default", "format": {"type": "image/png"}}],
        },
        "evalscript": (
            '//VERSION=3\n'
            'function setup(){return{input:[{bands:["B04","B03","B02","dataMask"]}],output:{bands:4}};}\n'
            'function evaluatePixel(s){return[2.5*s.B04,2.5*s.B03,2.5*s.B02,s.dataMask];}'
        ),
    }
    resp = requests.post(
        SH_PROCESS_URL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "image/png"},
        json=payload,
        timeout=30,
    )
    if resp.status_code == 200 and len(resp.content) > 100:
        print(f"  Tile received: {len(resp.content)} bytes (PNG)")
        return True
    else:
        print(f"  ERROR: status={resp.status_code}, body={resp.text[:200]}")
        return False


def list_byoc_collections(token: str) -> list:
    """List existing BYOC collections to find PlanetScope data."""
    resp = requests.get(
        "https://services.sentinel-hub.com/api/v1/byoc/collections",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def main():
    parser = argparse.ArgumentParser(description="Verify Sentinel Hub setup for mundi.ai")
    parser.add_argument("--client-id", default=os.environ.get("SH_CLIENT_ID", ""))
    parser.add_argument("--client-secret", default=os.environ.get("SH_CLIENT_SECRET", ""))
    args = parser.parse_args()

    if not args.client_id or not args.client_secret:
        print("ERROR: SH_CLIENT_ID and SH_CLIENT_SECRET required.")
        print()
        print("  export SH_CLIENT_ID=your-client-id")
        print("  export SH_CLIENT_SECRET=your-client-secret")
        print("  python3 scripts/setup_sentinel_hub.py")
        print()
        print("  Get credentials: https://apps.sentinel-hub.com/dashboard/#/account/settings")
        sys.exit(1)

    print("=" * 60)
    print("Sentinel Hub Verification — Process API")
    print("=" * 60)

    # Step 1: Authenticate
    print("\n[1/3] Authenticating...")
    token = get_token(args.client_id, args.client_secret)
    print("  OK")

    # Step 2: Test Sentinel-2 tile (available immediately)
    print("\n[2/3] Testing Sentinel-2 L2A tile (central Rwanda)...")
    s2_ok = test_s2_tile(token)

    # Step 3: Check PlanetScope BYOC collections
    print("\n[3/3] Checking PlanetScope BYOC collections...")
    collections = list_byoc_collections(token)
    if collections:
        print(f"  Found {len(collections)} BYOC collection(s):")
        for c in collections:
            print(f"    - {c['id']}: {c.get('name', 'unnamed')}")
    else:
        print("  No BYOC collections found.")
        print()
        print("  To import PlanetScope data for Rwanda:")
        print("    1. Go to https://apps.sentinel-hub.com/dashboard/#/tpdi")
        print("    2. Third Party Data Imports -> Planet -> Create Order")
        print("    3. Set AOI to Rwanda: [28.86, -2.84, 30.90, -1.05]")
        print("    4. Select PlanetScope product, date range")
        print("    5. After import completes, PlanetScope tiles will be available")

    # Results
    print()
    print("=" * 60)
    print("RESULTS")
    print("=" * 60)
    print()
    if s2_ok:
        print("  Sentinel-2 L2A:  WORKING (free, 10m, global)")
    else:
        print("  Sentinel-2 L2A:  FAILED")

    if collections:
        print(f"  PlanetScope:     {len(collections)} BYOC collection(s) available")
    else:
        print("  PlanetScope:     Not yet imported (needs BYOC/TPDI setup)")

    print()
    print("Add to .env or Render environment variables:")
    print()
    print(f"  SH_CLIENT_ID={args.client_id}")
    print(f"  SH_CLIENT_SECRET={args.client_secret}")
    print()
    print("  No SH_INSTANCE_ID needed — Process API sends evalscripts directly.")
    print()


if __name__ == "__main__":
    main()
