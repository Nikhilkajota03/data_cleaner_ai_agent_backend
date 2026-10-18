"""
Unit tests for Admin Authentication & Session Management.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app.core.config import settings
from backend.app.main import app


@pytest.mark.asyncio
async def test_admin_login_success():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["user"]["username"] == settings.ADMIN_USERNAME
        assert data["user"]["is_admin"] is True


@pytest.mark.asyncio
async def test_admin_login_with_email():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_EMAIL, "password": settings.ADMIN_PASSWORD},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data


@pytest.mark.asyncio
async def test_admin_login_invalid_password():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": "incorrect_password_123"},
        )
        assert resp.status_code == 401
        assert "Invalid" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_auth_me_protected():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Unauthenticated request must fail
        unauth_resp = await client.get("/api/auth/me")
        assert unauth_resp.status_code == 401

        # 2. Authenticated request must succeed
        login_resp = await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )
        token = login_resp.json()["access_token"]

        me_resp = await client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me_resp.status_code == 200
        assert me_resp.json()["username"] == settings.ADMIN_USERNAME
        assert me_resp.json()["role"] == "admin"
