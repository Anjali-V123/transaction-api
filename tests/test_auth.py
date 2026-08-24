def test_signup_creates_customer(client):
    resp = client.post("/auth/signup", json={"email": "new@example.com", "password": "password123"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "new@example.com"
    assert "password" not in body and "password_hash" not in body  # never echo the hash back


def test_signup_duplicate_email_rejected(client):
    client.post("/auth/signup", json={"email": "dupe@example.com", "password": "password123"})
    resp = client.post("/auth/signup", json={"email": "dupe@example.com", "password": "differentpass"})
    assert resp.status_code == 409


def test_signup_short_password_rejected(client):
    resp = client.post("/auth/signup", json={"email": "short@example.com", "password": "123"})
    assert resp.status_code == 422  # Pydantic's min_length=8 catches this before it ever hits the DB


def test_login_success_returns_token(client):
    client.post("/auth/signup", json={"email": "login@example.com", "password": "password123"})
    resp = client.post("/auth/login", json={"email": "login@example.com", "password": "password123"})
    assert resp.status_code == 200
    assert resp.json()["token_type"] == "bearer"
    assert len(resp.json()["access_token"]) > 20


def test_login_wrong_password_rejected(client):
    client.post("/auth/signup", json={"email": "wrongpass@example.com", "password": "password123"})
    resp = client.post("/auth/login", json={"email": "wrongpass@example.com", "password": "not-it"})
    assert resp.status_code == 401


def test_login_unknown_email_rejected(client):
    resp = client.post("/auth/login", json={"email": "nobody@example.com", "password": "password123"})
    assert resp.status_code == 401


def test_me_endpoint_requires_valid_token(client):
    resp = client.get("/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401


def test_me_endpoint_returns_current_customer(client):
    client.post("/auth/signup", json={"email": "me@example.com", "password": "password123"})
    token = client.post("/auth/login", json={"email": "me@example.com", "password": "password123"}).json()["access_token"]

    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "me@example.com"
