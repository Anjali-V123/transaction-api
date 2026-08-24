"""
Shared test fixtures. One engine, one dependency override, one `client`
fixture -- used by both test_auth.py and test_orders.py, so they can never
collide the way two independently-created engines in different test files
would (see room-booking's tests/conftest.py for the exact bug this avoids).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.database import Base, get_db
from app import models

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = _override_get_db


@pytest.fixture(autouse=True)
def reset_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def auth_header(client, email="cust1@example.com", password="password123"):
    """Sign up (or log in, if already registered) and return a ready-to-use Authorization header."""
    client.post("/auth/signup", json={"email": email, "password": password})
    token = client.post("/auth/login", json={"email": email, "password": password}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def cust1(client):
    return auth_header(client, "cust1@example.com")


@pytest.fixture
def cust2(client):
    return auth_header(client, "cust2@example.com")


@pytest.fixture
def admin(client):
    """
    Signs up a normal account, then reaches directly into the test DB to
    flip is_admin -- mirroring how scripts/make_admin.py promotes an
    account in real usage (there's no API for a customer to self-promote).
    """
    header = auth_header(client, "admin@example.com")
    db = TestingSessionLocal()
    try:
        customer = db.query(models.Customer).filter(models.Customer.email == "admin@example.com").first()
        customer.is_admin = True
        db.commit()
    finally:
        db.close()
    return header
