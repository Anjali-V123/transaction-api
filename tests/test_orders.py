import pytest


@pytest.fixture(autouse=True)
def seed_inventory(client, admin):
    client.post(
        "/inventory",
        json={"sku": "SKU1", "name": "Widget", "price": 10.0, "quantity_available": 5},
        headers=admin,
    )


def test_inventory_creation_requires_auth(client):
    resp = client.post(
        "/inventory",
        json={"sku": "SKU2", "name": "Gadget", "price": 5.0, "quantity_available": 1},
    )
    assert resp.status_code in (401, 403)  # HTTPBearer rejects a missing header before our own check runs


def test_inventory_creation_requires_admin(client, cust1):
    resp = client.post(
        "/inventory",
        json={"sku": "SKU2", "name": "Gadget", "price": 5.0, "quantity_available": 1},
        headers=cust1,
    )
    assert resp.status_code == 403


def test_order_requires_auth(client):
    resp = client.post(
        "/orders",
        json={"idempotency_key": "key-noauth", "sku": "SKU1", "quantity": 1},
    )
    assert resp.status_code in (401, 403)


def test_create_order_success(client, cust1):
    resp = client.post(
        "/orders",
        json={"idempotency_key": "key-1", "sku": "SKU1", "quantity": 2},
        headers=cust1,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "PENDING"
    assert data["total_amount"] == 20.0

    inv = client.get("/inventory").json()
    assert inv[0]["quantity_available"] == 3


def test_duplicate_idempotency_key_returns_same_order_without_double_decrement(client, cust1):
    payload = {"idempotency_key": "key-dup", "sku": "SKU1", "quantity": 1}
    r1 = client.post("/orders", json=payload, headers=cust1)
    r2 = client.post("/orders", json=payload, headers=cust1)

    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]

    inv = client.get("/inventory").json()
    assert inv[0]["quantity_available"] == 4  # decremented only once


def test_insufficient_inventory_rejected_and_no_partial_state(client, cust1):
    resp = client.post(
        "/orders",
        json={"idempotency_key": "key-2", "sku": "SKU1", "quantity": 100},
        headers=cust1,
    )
    assert resp.status_code == 400

    inv = client.get("/inventory").json()
    assert inv[0]["quantity_available"] == 5  # unchanged, no partial writes

    orders = client.get("/orders", headers=cust1).json()
    assert len(orders) == 0  # no order row created for the failed attempt


def test_unknown_sku_rejected(client, cust1):
    resp = client.post(
        "/orders",
        json={"idempotency_key": "key-unknown", "sku": "DOES-NOT-EXIST", "quantity": 1},
        headers=cust1,
    )
    assert resp.status_code == 404


def test_payment_failure_rolls_back_inventory(client, cust1):
    order = client.post(
        "/orders", json={"idempotency_key": "key-3", "sku": "SKU1", "quantity": 2}, headers=cust1
    ).json()

    inv = client.get("/inventory").json()
    assert inv[0]["quantity_available"] == 3

    pay_resp = client.post(f"/orders/{order['id']}/pay", json={"simulate_failure": True}, headers=cust1)
    assert pay_resp.status_code == 200
    assert pay_resp.json()["status"] == "FAILED"

    inv = client.get("/inventory").json()
    assert inv[0]["quantity_available"] == 5  # compensating rollback restored stock


def test_payment_success_marks_order_paid(client, cust1):
    order = client.post(
        "/orders", json={"idempotency_key": "key-4", "sku": "SKU1", "quantity": 1}, headers=cust1
    ).json()

    pay_resp = client.post(f"/orders/{order['id']}/pay", json={"simulate_failure": False}, headers=cust1)
    assert pay_resp.status_code == 200
    assert pay_resp.json()["status"] == "PAID"


def test_cannot_pay_an_already_settled_order(client, cust1):
    order = client.post(
        "/orders", json={"idempotency_key": "key-5", "sku": "SKU1", "quantity": 1}, headers=cust1
    ).json()
    client.post(f"/orders/{order['id']}/pay", json={"simulate_failure": False}, headers=cust1)

    resp = client.post(f"/orders/{order['id']}/pay", json={"simulate_failure": False}, headers=cust1)
    assert resp.status_code == 400


def test_cannot_pay_someone_elses_order(client, cust1, cust2):
    order = client.post(
        "/orders", json={"idempotency_key": "key-6", "sku": "SKU1", "quantity": 1}, headers=cust1
    ).json()

    resp = client.post(f"/orders/{order['id']}/pay", json={"simulate_failure": False}, headers=cust2)
    assert resp.status_code == 403


def test_orders_list_requires_auth(client):
    assert client.get("/orders").status_code in (401, 403)


def test_customer_sees_only_own_orders(client, cust1, cust2):
    client.post("/orders", json={"idempotency_key": "k-a", "sku": "SKU1", "quantity": 1}, headers=cust1)
    order2 = client.post(
        "/orders", json={"idempotency_key": "k-b", "sku": "SKU1", "quantity": 1}, headers=cust2
    ).json()

    mine = client.get("/orders", headers=cust1).json()
    assert len(mine) == 1 and mine[0]["id"] != order2["id"]
    assert client.get(f"/orders/{order2['id']}", headers=cust1).status_code == 404


def test_idempotency_key_cannot_expose_another_customers_order(client, cust1, cust2):
    payload = {"idempotency_key": "shared-key", "sku": "SKU1", "quantity": 1}
    first = client.post("/orders", json=payload, headers=cust1)
    assert first.status_code == 201

    replay_by_other = client.post("/orders", json=payload, headers=cust2)
    assert replay_by_other.status_code == 409
    assert "id" not in replay_by_other.json()
