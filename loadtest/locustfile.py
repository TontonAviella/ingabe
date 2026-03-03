"""Load testing baselines for Mundi.ai using Locust.

Usage:
    pip install locust
    locust -f loadtest/locustfile.py --host http://localhost:8000

    # Headless mode (CI-friendly):
    locust -f loadtest/locustfile.py --host http://localhost:8000 \
        --headless -u 50 -r 5 --run-time 60s
"""

from locust import HttpUser, task, between, tag


class HealthCheckUser(HttpUser):
    """Baseline: health and readiness probes."""

    wait_time = between(1, 3)
    weight = 1

    @tag("health")
    @task(3)
    def health_check(self):
        self.client.get("/healthz")

    @tag("health")
    @task(1)
    def detailed_health(self):
        self.client.get("/health")

    @tag("health")
    @task(1)
    def readiness(self):
        self.client.get("/ready")

    @tag("metrics")
    @task(1)
    def metrics(self):
        self.client.get("/metrics")


class BrowsingUser(HttpUser):
    """Simulates a user browsing maps and projects."""

    wait_time = between(2, 5)
    weight = 5

    @tag("spa")
    @task(3)
    def load_spa(self):
        """Load the SPA index page."""
        self.client.get("/", name="/[SPA]")

    @tag("api", "projects")
    @task(2)
    def list_projects(self):
        """List all projects."""
        self.client.get("/api/projects/")

    @tag("api", "maps")
    @task(2)
    def list_maps(self):
        """List maps (requires a project — uses default)."""
        with self.client.get(
            "/api/maps/",
            catch_response=True,
        ) as response:
            # 401/403 is expected without auth — mark as success for baseline
            if response.status_code in (401, 403, 422):
                response.success()

    @tag("api", "basemaps")
    @task(1)
    def list_basemaps(self):
        """List available basemaps."""
        self.client.get("/api/basemaps/")


class MapViewerUser(HttpUser):
    """Simulates a user viewing a map and loading tiles."""

    wait_time = between(1, 3)
    weight = 3

    @tag("api", "maps")
    @task(2)
    def load_map_style(self):
        """Attempt to load a map style (will 404 without valid map ID — baseline)."""
        with self.client.get(
            "/api/maps/test-map/style.json",
            name="/api/maps/[id]/style.json",
            catch_response=True,
        ) as response:
            if response.status_code in (404, 401, 403):
                response.success()

    @tag("health")
    @task(1)
    def health_during_load(self):
        self.client.get("/healthz")
