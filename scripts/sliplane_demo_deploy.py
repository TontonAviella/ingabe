#!/usr/bin/env python3
"""Create a disposable Sliplane demo deployment for Mundi.

This intentionally provisions only the services needed for a short demo:
Postgres, Redis, MinIO, and the main FastAPI app. It avoids Dagster, Superset,
Grafana, backups, senders, and QGIS by default so a demo server has a chance.

Required:
  SLIPLANE_TOKEN=api_rw_org_...

Optional:
  SLIPLANE_PROJECT_NAME=ingabe-demo
  SLIPLANE_SERVER_ID=server_...
  SLIPLANE_REPO_URL=https://github.com/TontonAviella/ingabe.git
  SLIPLANE_BRANCH=main
  POSTGRES_PASSWORD=...
  S3_SECRET_ACCESS_KEY=...
  OPENAI_API_KEY=...
  CLERK_SECRET_KEY=...
  CLERK_PUBLISHABLE_KEY=...
  CLERK_ISSUER=...
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any


API = "https://ctrl.sliplane.io/v0"


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


TOKEN = env("SLIPLANE_TOKEN")
if not TOKEN:
    sys.exit("Set SLIPLANE_TOKEN to a Sliplane read/write API token.")


def request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read()
            if not body:
                return None
            return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} failed: HTTP {exc.code}: {detail}") from exc


def env_vars(values: dict[str, str], secret_keys: set[str] | None = None) -> list[dict[str, Any]]:
    secret_keys = secret_keys or set()
    return [
        {"key": key, "value": value, "secret": key in secret_keys}
        for key, value in values.items()
        if value != ""
    ]


def find_by_name(items: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((item for item in items if item.get("name") == name), None)


def ensure_project(name: str) -> dict[str, Any]:
    project = find_by_name(request("GET", "/projects"), name)
    if project:
        print(f"project: {name} ({project['id']})")
        return project
    project = request("POST", "/projects", {"name": name})
    print(f"project: created {name} ({project['id']})")
    return project


def choose_server() -> dict[str, Any]:
    server_id = env("SLIPLANE_SERVER_ID")
    servers = request("GET", "/servers")
    if server_id:
        for server in servers:
            if server.get("id") == server_id:
                print(f"server: {server['name']} ({server['id']})")
                return server
        sys.exit(f"SLIPLANE_SERVER_ID={server_id} was not found.")
    if not servers:
        sys.exit("No Sliplane server found. Create a demo server in the dashboard first.")
    running = [s for s in servers if s.get("status") == "running"]
    server = running[0] if running else servers[0]
    print(f"server: {server['name']} ({server['id']}, {server.get('instanceType')})")
    return server


def services(project_id: str) -> list[dict[str, Any]]:
    return request("GET", f"/projects/{project_id}/services")


def ensure_service(project_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    existing = find_by_name(services(project_id), spec["name"])
    if existing:
        print(f"service: {spec['name']} exists ({existing['id']})")
        return existing
    service = request("POST", f"/projects/{project_id}/services", spec)
    print(f"service: created {spec['name']} ({service['id']})")
    return service


def internal_domain(service: dict[str, Any]) -> str:
    return service["network"]["internalDomain"]


def wait_for_domains(project_id: str, names: list[str], timeout_sec: int = 60) -> dict[str, dict[str, Any]]:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        by_name = {svc["name"]: svc for svc in services(project_id)}
        if all(by_name.get(name, {}).get("network", {}).get("internalDomain") for name in names):
            return {name: by_name[name] for name in names}
        time.sleep(3)
    sys.exit(f"Timed out waiting for internal domains: {', '.join(names)}")


def main() -> None:
    project = ensure_project(env("SLIPLANE_PROJECT_NAME", "ingabe-demo"))
    server = choose_server()
    server_id = server["id"]

    repo_url = env("SLIPLANE_REPO_URL", "https://github.com/TontonAviella/ingabe.git")
    branch = env("SLIPLANE_BRANCH", "main")
    pg_password = env("POSTGRES_PASSWORD", "demo-postgres-change-me")
    s3_secret = env("S3_SECRET_ACCESS_KEY", "demo-s3-change-me")
    bucket = env("S3_BUCKET", "ingabe-demo")

    postgres = ensure_service(
        project["id"],
        {
            "name": "postgres",
            "serverId": server_id,
            "deployment": {
                "url": repo_url,
                "branch": branch,
                "dockerContext": ".",
                "dockerfilePath": "Dockerfile.postgres",
                "autoDeploy": False,
                "includePaths": ["Dockerfile.postgres"],
            },
            "network": {"public": False},
            "env": env_vars(
                {
                    "POSTGRES_DB": "mundidb",
                    "POSTGRES_USER": "mundiuser",
                    "POSTGRES_PASSWORD": pg_password,
                },
                {"POSTGRES_PASSWORD"},
            ),
            "volumes": [{"name": "postgres-data", "mountPath": "/var/lib/postgresql/data"}],
        },
    )

    redis = ensure_service(
        project["id"],
        {
            "name": "redis",
            "serverId": server_id,
            "deployment": {"url": "docker.io/library/redis:alpine"},
            "network": {"public": False},
            "cmd": "redis-server --maxmemory 512mb --maxmemory-policy allkeys-lru",
        },
    )

    minio = ensure_service(
        project["id"],
        {
            "name": "minio",
            "serverId": server_id,
            "deployment": {"url": "docker.io/bitnamilegacy/minio:latest"},
            "network": {"public": False},
            "env": env_vars(
                {
                    "MINIO_ROOT_USER": "admin",
                    "MINIO_ROOT_PASSWORD": s3_secret,
                    "MINIO_DEFAULT_BUCKETS": bucket,
                },
                {"MINIO_ROOT_PASSWORD"},
            ),
            "volumes": [{"name": "minio-data", "mountPath": "/data"}],
        },
    )

    domains = wait_for_domains(project["id"], ["postgres", "redis", "minio"])
    postgres_host = internal_domain(domains["postgres"])
    redis_host = internal_domain(domains["redis"])
    minio_endpoint = f"http://{internal_domain(domains['minio'])}:9000"

    app_env = {
        "PORT": "8000",
        "WEBSITE_DOMAIN": env("WEBSITE_DOMAIN", ""),
        "MUNDI_AUTH_MODE": env("MUNDI_AUTH_MODE", "edit"),
        "POSTGRES_HOST": postgres_host,
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "mundidb",
        "POSTGRES_USER": "mundiuser",
        "POSTGRES_PASSWORD": pg_password,
        "REDIS_HOST": redis_host,
        "REDIS_PORT": "6379",
        "S3_ACCESS_KEY_ID": "admin",
        "S3_SECRET_ACCESS_KEY": s3_secret,
        "S3_DEFAULT_REGION": "us-east-1",
        "S3_ENDPOINT_URL": minio_endpoint,
        "S3_BUCKET": bucket,
        "OPENAI_API_KEY": env("OPENAI_API_KEY"),
        "OPENAI_BASE_URL": env("OPENAI_BASE_URL"),
        "OPENAI_MODEL": env("OPENAI_MODEL", "gpt-4.1-nano"),
        "CLERK_SECRET_KEY": env("CLERK_SECRET_KEY"),
        "CLERK_PUBLISHABLE_KEY": env("CLERK_PUBLISHABLE_KEY"),
        "CLERK_ISSUER": env("CLERK_ISSUER"),
        "BRAIN_EMBEDDINGS_DISABLED": "true",
        "TILE_CACHE_ENABLED": "true",
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "POSTGIS_LOCALHOST_POLICY": "docker_rewrite",
        "WEB_CONCURRENCY": env("WEB_CONCURRENCY", "1"),
        "DB_POOL_MAX_SIZE": env("DB_POOL_MAX_SIZE", "5"),
        "RATE_LIMIT_ENABLED": "false",
    }

    app = ensure_service(
        project["id"],
        {
            "name": "app",
            "serverId": server_id,
            "deployment": {
                "url": repo_url,
                "branch": branch,
                "dockerContext": ".",
                "dockerfilePath": "Dockerfile",
                "autoDeploy": False,
                "includePaths": [
                    "Dockerfile",
                    "Dockerfile.base",
                    "requirements.txt",
                    "src/**",
                    "alembic/**",
                    "frontendts/**",
                    "scripts/**",
                    "services/**",
                    "clay-source/**",
                ],
            },
            "network": {"public": True, "protocol": "http"},
            "healthcheck": "/healthz",
            "cmd": (
                "bash -lc 'export PATH=/app/.venv/bin:$PATH && "
                "/app/.venv/bin/python -m alembic upgrade head && "
                "/app/.venv/bin/python -m uvicorn src.wsgi:app --host 0.0.0.0 --port ${PORT:-8000} "
                "--workers ${WEB_CONCURRENCY:-1} --log-level info "
                "--proxy-headers --forwarded-allow-ips=\"*\"'"
            ),
            "env": env_vars(
                app_env,
                {"POSTGRES_PASSWORD", "S3_SECRET_ACCESS_KEY", "OPENAI_API_KEY", "CLERK_SECRET_KEY"},
            ),
        },
    )

    current = {svc["name"]: svc for svc in services(project["id"])}
    app = current.get("app", app)
    managed = app.get("network", {}).get("managedDomain", "")
    print("\nDemo services requested.")
    print(f"App service id: {app['id']}")
    if managed:
        print(f"App URL: https://{managed}")
    else:
        print("App URL: check the Sliplane dashboard once the public domain is assigned.")
    print("\nImportant: demo servers are deleted after 48h unless you add a payment method.")


if __name__ == "__main__":
    main()
