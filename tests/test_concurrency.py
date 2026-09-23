"""
Concurrent orders against a real Postgres. Skipped unless
CONCURRENCY_TEST_DATABASE_URL is set, e.g.
    postgresql://app:app@localhost:5432/transactions_test
"""
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DB_URL = os.getenv("CONCURRENCY_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="needs a real Postgres")


@pytest.fixture
def pg_client():
    from app.database import Base, get_db
    from app.main import app
    from app import models

    engine = create_engine(DB_URL, pool_size=30)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override
    yield TestClient(app), Session, models
    if previous:
        app.dependency_overrides[get_db] = previous
    engine.dispose()


def _token(client, email):
    client.post("/auth/signup", json={"email": email, "password": "password123"})
    return client.post("/auth/login", json={"email": email, "password": "password123"}).json()["access_token"]


def test_concurrent_orders_never_oversell(pg_client):
    client, Session, models = pg_client
    STOCK, N = 5, 12

    admin = _token(client, "admin@example.com")
    with Session() as db:
        db.query(models.Customer).filter_by(email="admin@example.com").update({"is_admin": True})
        db.commit()
    client.post("/inventory", json={"sku": "HOT", "name": "Hot item", "price": 10.0, "quantity_available": STOCK},
                headers={"Authorization": f"Bearer {admin}"})

    tokens = [_token(client, f"c{i}@example.com") for i in range(N)]
    barrier = threading.Barrier(N)

    def attempt(i):
        with TestClient(client.app) as c:
            barrier.wait()
            return c.post("/orders", json={"idempotency_key": str(uuid.uuid4()), "sku": "HOT", "quantity": 1},
                          headers={"Authorization": f"Bearer {tokens[i]}"})

    with ThreadPoolExecutor(max_workers=N) as pool:
        responses = list(pool.map(attempt, range(N)))

    successes = sum(r.status_code == 201 for r in responses)
    with Session() as db:
        remaining = db.get(models.InventoryItem, "HOT").quantity_available
        orders = db.query(models.Order).count()

    # Stock accounting must balance: every successful order took exactly one unit.
    assert successes == orders
    assert successes == STOCK, f"{successes} orders succeeded for {STOCK} units in stock"
    assert remaining == 0, f"remaining={remaining} after {successes} orders"
