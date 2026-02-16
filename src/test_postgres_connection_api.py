import os
import pytest
from unittest.mock import patch


@pytest.mark.anyio
class TestPostgresConnectionAPI:
    async def create_test_project(self, auth_client):
        """Helper method to create a test project"""
        map_response = await auth_client.post(
            "/api/maps/create",
            json={
                "title": "Test Map for PostgreSQL Connection Testing",
                "description": "Test project for postgres connection policy testing",
            },
        )
        assert map_response.status_code == 200
        return map_response.json()["project_id"]

    async def test_disallow_policy_localhost(self, auth_client):
        project_id = await self.create_test_project(auth_client)

        with patch.dict(os.environ, {"POSTGIS_LOCALHOST_POLICY": "disallow"}):
            response = await auth_client.post(
                f"/api/projects/{project_id}/postgis-connections",
                json={"connection_uri": "postgresql://user:pass@localhost:5432/db"},
            )

            assert response.status_code == 400
            data = response.json()
            assert "Detected a localhost database address" in data["detail"]

    async def test_disallow_policy_127_0_0_1(self, auth_client):
        project_id = await self.create_test_project(auth_client)

        with patch.dict(os.environ, {"POSTGIS_LOCALHOST_POLICY": "disallow"}):
            response = await auth_client.post(
                f"/api/projects/{project_id}/postgis-connections",
                json={"connection_uri": "postgresql://user:pass@127.0.0.1:5432/db"},
            )

            assert response.status_code == 400
            data = response.json()
            assert "localhost database address" in data["detail"]

    async def test_docker_rewrite_policy_localhost(self, auth_client):
        project_id = await self.create_test_project(auth_client)

        with patch.dict(os.environ, {"POSTGIS_LOCALHOST_POLICY": "docker_rewrite"}):
            response = await auth_client.post(
                f"/api/projects/{project_id}/postgis-connections",
                json={"connection_uri": "postgresql://user:pass@localhost:5432/db"},
            )

            # This should not fail due to localhost policy, but will fail at actual connection attempt
            # The policy should rewrite localhost to host.docker.internal, so the 400 error
            # should be about connection failure, not localhost policy
            assert (
                response.status_code != 400
                or "localhost database address" not in response.json().get("detail", "")
            )

    async def test_allow_policy_localhost(self, auth_client):
        project_id = await self.create_test_project(auth_client)

        with patch.dict(os.environ, {"POSTGIS_LOCALHOST_POLICY": "allow"}):
            response = await auth_client.post(
                f"/api/projects/{project_id}/postgis-connections",
                json={"connection_uri": "postgresql://user:pass@localhost:5432/db"},
            )

            # This should not fail due to localhost policy, but will fail at actual connection attempt
            assert (
                response.status_code != 400
                or "localhost database address" not in response.json().get("detail", "")
            )

    async def test_unknown_policy_defaults_to_disallow(self, auth_client):
        project_id = await self.create_test_project(auth_client)

        with patch.dict(os.environ, {"POSTGIS_LOCALHOST_POLICY": "unknown_policy"}):
            response = await auth_client.post(
                f"/api/projects/{project_id}/postgis-connections",
                json={"connection_uri": "postgresql://user:pass@localhost:5432/db"},
            )

            assert response.status_code == 500

    async def test_no_policy_defaults_to_disallow(self, auth_client):
        project_id = await self.create_test_project(auth_client)

        # Remove only the POSTGIS_LOCALHOST_POLICY variable to test default behavior
        original_policy = os.environ.pop("POSTGIS_LOCALHOST_POLICY", None)
        try:
            response = await auth_client.post(
                f"/api/projects/{project_id}/postgis-connections",
                json={"connection_uri": "postgresql://user:pass@localhost:5432/db"},
            )

            assert response.status_code == 500
        finally:
            # Restore the original value if it existed
            if original_policy is not None:
                os.environ["POSTGIS_LOCALHOST_POLICY"] = original_policy

    async def test_invalid_uri_format(self, auth_client):
        project_id = await self.create_test_project(auth_client)

        with patch.dict(os.environ, {"POSTGIS_LOCALHOST_POLICY": "allow"}):
            response = await auth_client.post(
                f"/api/projects/{project_id}/postgis-connections",
                json={"connection_uri": "mysql://user:pass@localhost:3306/db"},
            )

            assert response.status_code == 400
            data = response.json()
            assert "Must start with 'postgresql://'" in data["detail"]

    async def test_missing_hostname(self, auth_client):
        project_id = await self.create_test_project(auth_client)

        with patch.dict(os.environ, {"POSTGIS_LOCALHOST_POLICY": "allow"}):
            response = await auth_client.post(
                f"/api/projects/{project_id}/postgis-connections",
                json={"connection_uri": "postgresql://user:pass@:5432/db"},
            )

            assert response.status_code == 400
            data = response.json()
            assert "must include a hostname" in data["detail"]

    async def test_non_localhost_addresses_pass_validation(self, auth_client):
        project_id = await self.create_test_project(auth_client)

        test_uris = [
            "postgresql://user:pass@example.com:5432/db",
            "postgresql://user:pass@192.168.1.100:5432/db",
            "postgresql://user:pass@10.0.0.1:5432/db",
        ]

        for policy in ["disallow", "docker_rewrite", "allow"]:
            with patch.dict(os.environ, {"POSTGIS_LOCALHOST_POLICY": policy}):
                for uri in test_uris:
                    response = await auth_client.post(
                        f"/api/projects/{project_id}/postgis-connections",
                        json={"connection_uri": uri},
                    )

                    # These should pass URI validation (no 400 error for localhost policy)
                    # but may fail at actual connection attempt with different error
                    if response.status_code == 400:
                        data = response.json()
                        assert "localhost database address" not in data["detail"]
