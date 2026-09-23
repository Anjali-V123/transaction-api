# Transaction Processing API

A full-stack order/payment system: a FastAPI backend, a React dashboard,
and a Docker Compose setup that runs it as two load-balanced replicas
behind nginx with a shared Postgres database and a Redis cache. Built to
demonstrate the correctness and scaling concerns real transaction systems
care about, not just CRUD.

## Why this design

**Authentication and authorization.** Every write (creating inventory,
placing an order, paying an order) requires a logged-in customer — JWT
tokens, bcrypt-hashed passwords. Who's placing an order is always derived
from the caller's token, never taken from the request body, so one
customer can't place an order as someone else. Paying an order checks
that the caller actually owns it (`403` otherwise) — you can't pay or
fail someone else's order just by knowing its ID.

Placing orders and managing inventory are different roles, not just
different endpoints: any logged-in customer can order, but only an
account with `is_admin` set can add inventory (`403` otherwise) — the
same distinction a real storefront makes between a shopper and whoever
stocks the warehouse. There's no signup-time or API way to become an
admin; every new account starts as a regular customer, and promoting one
is a deliberate operator action run directly against the database
(`python -m scripts.make_admin <email>`) rather than something exposed
over the public API.

**Idempotency.** A client retries a timed-out request, or a network blip
causes a double-submit. Without protection, that's a double-charge or a
double-decrement of stock. Every order requires an `idempotency_key`;
replaying the same key returns the original order instead of creating a
second one.

**Atomicity.** If inventory gets decremented but the order fails to save
(or a payment later fails), the system must never be left in an
inconsistent state. Every state change happens inside one DB transaction
that either fully commits or fully rolls back. A failed payment runs a
*compensating* transaction: it restores the reserved inventory and marks
the order `FAILED`, atomically.

**Horizontal scaling.** The API runs as two identical, stateless replicas
(`api1`, `api2`) behind an nginx load balancer, both sharing the same
Postgres database and Redis cache. Because neither replica holds any
state of its own, either one can serve any request — adding a third
replica is a one-line change to `nginx.conf`. `GET /inventory` is cached
in Redis (cache-aside, 15s TTL) and invalidated on every write that
changes stock, so a read on `api2` immediately reflects a write that just
happened on `api1`.

## A real bug this project hit (and the fix)

Building the two-replica setup surfaced an actual race condition, not a
hypothetical one: starting `api1` and `api2` at once against a *fresh*
Postgres database crashed one of them. Both processes ran
`Base.metadata.create_all()` on boot, both saw the `OrderStatus` enum
type didn't exist yet, and both issued `CREATE TYPE` — Postgres let one
through and threw a `UniqueViolation` at the other.

The fix is the standard one: schema creation doesn't belong inside app
boot when multiple replicas can start concurrently. `app/init_db.py` is
now a separate one-shot script that creates the schema exactly once,
before either replica starts — `docker-compose.yml` runs it as a
`migrate` service that `api1` and `api2` both wait on
(`service_completed_successfully`). `main.py` still calls `create_all()`
for convenience in single-instance local dev, but it's wrapped to survive
the race rather than crash if it ever does run concurrently.

## A second real bug: overselling under concurrent orders

Order creation originally read an item's stock, subtracted in Python, and
wrote it back. Under concurrency that's a classic lost update: several
requests read the same quantity and each writes back its own result. A
real-Postgres test (`tests/test_concurrency.py`) that releases 12
simultaneous orders at an item with 5 units in stock showed **all 12
succeeding**.

The fix is a row-level lock: order creation now loads the inventory row
with `SELECT ... FOR UPDATE` (`with_for_update()` in SQLAlchemy), so
concurrent orders for the same SKU queue on that row until the previous
transaction commits. Payment does the same on the order row, so two
concurrent `/pay` calls can't both see `PENDING` and restore stock twice.
The same test now shows exactly 5 orders succeed and stock ends at 0.

The same review closed an authorization gap: `GET /orders` and
`GET /orders/{id}` were public. Both now require a token; customers see
only their own orders (someone else's order returns `404`, so IDs can't
be probed) and admins see all.

## Tech stack

- **FastAPI** + **SQLAlchemy** — REST API, transaction management
- **Auth:** JWT (PyJWT) + bcrypt password hashing
- **React** (Vite) — order/inventory dashboard, built and served as
  static files directly by FastAPI (one deployable unit, no separate
  frontend host needed)
- **PostgreSQL** — shared by both replicas; **SQLite** as a zero-setup
  fallback for single-instance local dev
- **Redis** — cache-aside on the read-heavy inventory endpoint, shared
  across replicas
- **nginx** — round-robin load balancer across two API replicas
- **Docker Compose** — postgres + redis + migrate + api1 + api2 + nginx,
  one command to run the whole system
- **pytest** — 22 tests: signup/login/auth (8), orders/payments (13), and a
  real-Postgres concurrent-ordering test (1)
  covering success path, idempotent replay, insufficient inventory,
  unknown SKU, payment failure with rollback, payment success,
  double-payment rejection, cross-customer authorization, order
  visibility, and admin-only inventory management

## Running the full system

Requires [Docker Desktop](https://www.docker.com/products/docker-desktop/).

```bash
docker compose up --build
```

Open **http://localhost:8080** — that's nginx, load-balancing across
`api1` and `api2`. The dashboard shows a `served by: apiN` badge that
alternates as requests land on different replicas; watch it change as you
refresh or as the page polls.

To *prove* the load balancing yourself:

```bash
for i in {1..6}; do curl -s localhost:8080/health; echo; done
# alternates {"status":"ok","instance":"api1"} / "api2"
```

## Running just the backend locally (no Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://localhost:8000/docs for interactive API docs (uses local
SQLite automatically — no Postgres needed for this path).

To run the dashboard against it in dev mode:

```bash
cd frontend
npm install
npm run dev
```

## Running the tests

```bash
source .venv/bin/activate
pytest tests/ -v
```

The concurrency test needs a real Postgres and is skipped otherwise:

```bash
docker run -d --name pgtest -e POSTGRES_USER=app -e POSTGRES_PASSWORD=app \
  -e POSTGRES_DB=transactions_test -p 5433:5432 postgres:16-alpine
CONCURRENCY_TEST_DATABASE_URL=postgresql://app:app@localhost:5433/transactions_test pytest tests/ -v
```

## Promoting an account to admin

Inventory management (`POST /inventory`) requires an admin account. Sign
up normally through the app first, then run:

```bash
python -m scripts.make_admin you@example.com
```

This updates the account directly in whichever database `DATABASE_URL`
points to (or the local SQLite file if unset). There's no in-app way to
do this on purpose — self-service admin promotion would defeat the point
of restricting the endpoint.

## Deployment

Not currently deployed. The docker-compose setup above (postgres + redis +
migrate + two API replicas + nginx) is the real, working multi-container
story, but it doesn't map onto most free single-container hosting tiers
without dropping the load balancer and running one instance — which would
mean deploying the least interesting part of this project. Kept local for
now.

## API endpoints

| Method | Path                      | Auth required | Description                                   |
|--------|---------------------------|:--:|------------------------------------------------|
| POST   | `/auth/signup`            |    | Create a customer account                      |
| POST   | `/auth/login`              |    | Log in, returns a JWT                          |
| GET    | `/auth/me`                | ✓  | Current logged-in customer                     |
| GET    | `/health`                 |    | Health check (returns which replica answered)  |
| POST   | `/inventory`               | ✓ (admin) | Create an inventory item                |
| GET    | `/inventory`               |    | List inventory (Redis-cached, 15s TTL)         |
| POST   | `/orders`                  | ✓  | Create an order (idempotent, atomic)           |
| GET    | `/orders`                  |    | List all orders                                |
| GET    | `/orders/{order_id}`       |    | Get one order                                  |
| POST   | `/orders/{order_id}/pay`   | ✓  | Pay your own order (403 otherwise)             |

## Possible extensions (not built, but natural next steps)

- Rate limiting per customer
- A real message queue for async payment processing instead of a
  synchronous endpoint
- Multi-item orders (currently one SKU per order, for scope reasons)
