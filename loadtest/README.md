# Load Testing

Locust-based load testing for Mundi.ai.

## Quick Start

```bash
pip install locust
locust -f loadtest/locustfile.py --host http://localhost:8000
```

Open http://localhost:8089 in your browser to configure and run tests.

## Headless Mode (CI)

```bash
locust -f loadtest/locustfile.py --host http://localhost:8000 \
    --headless -u 50 -r 5 --run-time 60s \
    --csv=loadtest/results
```

## User Profiles

| Profile | Weight | Description |
|---------|--------|-------------|
| HealthCheckUser | 1 | Probes /healthz, /health, /ready, /metrics |
| BrowsingUser | 5 | SPA load, project/map listing |
| MapViewerUser | 3 | Map style loading, tile requests |

## Baseline Targets

| Metric | Target |
|--------|--------|
| p95 latency | < 500ms |
| Error rate | < 1% |
| Throughput | > 100 req/s at 50 users |
