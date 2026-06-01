# Sliplane Demo Deployment

Goal: create a disposable 48-hour Sliplane demo for the Wednesday pitch without
trying to run the full Hetzner stack.

## What This Deploys

- `postgres`: PostGIS + pgvector from `Dockerfile.postgres`
- `redis`: `redis:alpine`
- `minio`: S3-compatible object storage for demo uploads
- `app`: main Mundi FastAPI/frontend container from `Dockerfile`

It intentionally skips QGIS, Dagster, Superset, Grafana, senders, and backups.

## Before Running

1. In Sliplane, create a demo server from the dashboard.
2. In Sliplane team settings, create a read/write API token.
3. Make sure the branch you deploy exists on GitHub.

Demo servers are deleted 48 hours after creation unless a payment method is
added. Do not store irreplaceable data there.

## Run

```bash
cd /Users/macbook/Ingabe/mundi.ai

export SLIPLANE_TOKEN='api_rw_org_...'
export SLIPLANE_PROJECT_NAME='ingabe-demo'
export SLIPLANE_REPO_URL='https://github.com/TontonAviella/ingabe.git'
export SLIPLANE_BRANCH='main'

export POSTGRES_PASSWORD='replace-with-demo-password'
export S3_SECRET_ACCESS_KEY='replace-with-demo-s3-secret'

# Optional, only if Sage needs to call OpenAI in the demo:
export OPENAI_API_KEY='...'
export OPENAI_MODEL='gpt-4.1-nano'

./scripts/sliplane_demo_deploy.py
```

If more than one Sliplane server exists, set:

```bash
export SLIPLANE_SERVER_ID='server_...'
```

## After Running

Watch Sliplane build logs for:

- `postgres` live
- `redis` live
- `minio` live
- `app` live

The app health check is `/healthz`. The detailed `/health` endpoint may report
QGIS down because this demo deployment skips QGIS on purpose.

## Demo Constraints

- Keep uploads tiny.
- Avoid QGIS-specific prompts/tools.
- Avoid Clay visual similarity unless Qdrant and model files are explicitly
  added later.
- Have local tunnel fallback ready from the Mac.
