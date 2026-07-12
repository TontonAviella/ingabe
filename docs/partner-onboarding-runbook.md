# Local Partner Onboarding Runbook

Ingabe runs on the local Docker stack. GitHub stores source code and runs CI;
it is not the application runtime.

## Start And Verify

```bash
git clone https://github.com/TontonAviella/ingabe.git
cd ingabe
scripts/deploy.sh
scripts/runtime-audit.sh --deep
```

Open `http://localhost:8000`. For frontend development, run the Vite server
from `frontendts` and open `http://localhost:5173`.

## Data Boundary

- PostgreSQL/PostGIS, Redis, MinIO, QGIS processing, rasterd, geokernel,
  Dagster, FastSAM, and GeoLibre run locally through `docker-compose.yml`.
- Uploaded orthophotos and generated artifacts remain in local MinIO and
  local database volumes.
- Do not configure SSH deployment hosts, remote Compose overrides, or public
  object-storage endpoints for this workflow.

## Release Boundary

- Push reviewed commits to `TontonAviella/ingabe`.
- GitHub Actions verifies builds, backend tests, frontend checks, and E2E flows.
- A passing GitHub run proves the source revision; `scripts/deploy.sh
  --check-only` proves the local runtime currently serving the user.
