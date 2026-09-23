import os
from typing import List

import jwt
from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import auth, cache, models, schemas
from .database import Base, engine, get_db

# Schema creation is normally handled once, up front, by `python -m
# app.init_db` (see docker-compose.yml's one-shot `migrate` service) --
# NOT here on every app boot. Calling create_all() from every replica at
# once is a real race on Postgres: two processes can both see the
# OrderStatus enum type doesn't exist yet and both issue CREATE TYPE,
# and the loser crashes with an IntegrityError on pg_type. This call
# stays as a convenience for single-instance local dev
# (`uvicorn app.main:app --reload`) and is made resilient to that race so
# it never crashes a replica that loses it -- whichever process wins
# creates the schema, the other just proceeds against what's already
# there.
try:
    Base.metadata.create_all(bind=engine)
except IntegrityError:
    pass

# Distinguishes which replica served a request -- set differently per
# container in docker-compose.yml so you can prove the load balancer is
# actually distributing traffic (curl the nginx port repeatedly and watch
# this value alternate between replicas).
INSTANCE_NAME = os.getenv("INSTANCE_NAME", "local")

app = FastAPI(
    title="Transaction Processing API",
    description=(
        "A small order/payment pipeline demonstrating idempotent request "
        "handling and atomic rollback on failure."
    ),
    version="1.0.0",
)


security = HTTPBearer()


def get_current_customer(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> models.Customer:
    try:
        payload = auth.decode_access_token(credentials.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired, please log in again")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid authentication token")

    customer = db.get(models.Customer, payload.get("sub"))
    if not customer:
        raise HTTPException(status_code=401, detail="Customer no longer exists")
    return customer


@app.post("/auth/signup", response_model=schemas.CustomerOut, status_code=201)
def signup(customer_in: schemas.CustomerCreate, db: Session = Depends(get_db)):
    existing = db.query(models.Customer).filter(models.Customer.email == customer_in.email).first()
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    customer = models.Customer(
        email=customer_in.email,
        password_hash=auth.hash_password(customer_in.password),
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


@app.post("/auth/login", response_model=schemas.Token)
def login(credentials: schemas.LoginRequest, db: Session = Depends(get_db)):
    customer = db.query(models.Customer).filter(models.Customer.email == credentials.email).first()
    # Same error for "no such email" and "wrong password" -- deliberately,
    # so a failed login attempt can't be used to discover which emails
    # are registered.
    if not customer or not auth.verify_password(credentials.password, customer.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password")

    token = auth.create_access_token(customer.id, customer.email)
    return schemas.Token(access_token=token)


@app.get("/auth/me", response_model=schemas.CustomerOut)
def read_current_customer(current_customer: models.Customer = Depends(get_current_customer)):
    return current_customer


@app.get("/health")
def health():
    return {"status": "ok", "instance": INSTANCE_NAME}


@app.post("/inventory", response_model=schemas.InventoryOut, status_code=201)
def create_inventory(
    item: schemas.InventoryCreate,
    db: Session = Depends(get_db),
    current_customer: models.Customer = Depends(get_current_customer),
):
    # Any authenticated customer can place orders, but managing inventory is
    # a store-owner action, not a customer one -- gated on is_admin rather
    # than just "logged in". There's no signup-time way to become an admin
    # (see scripts/make_admin.py); every account starts as a regular
    # customer.
    if not current_customer.is_admin:
        raise HTTPException(status_code=403, detail="Only admin accounts can manage inventory")

    existing = db.get(models.InventoryItem, item.sku)
    if existing:
        raise HTTPException(status_code=409, detail="SKU already exists")
    db_item = models.InventoryItem(**item.model_dump())
    db.add(db_item)
    db.commit()
    db.refresh(db_item)
    cache.invalidate_inventory_cache()
    return db_item


@app.get("/inventory", response_model=List[schemas.InventoryOut])
def list_inventory(db: Session = Depends(get_db)):
    """
    Cache-aside read: serve from Redis when warm (shared across all LB'd
    replicas, so any instance's write is visible to every instance's cached
    reads once the cache is invalidated), fall back to Postgres on a miss,
    and repopulate the cache. Any write that changes inventory
    (order creation, a failed payment restoring stock) invalidates this key
    so reads are never stale for longer than one write.
    """
    cached = cache.get_cached_inventory()
    if cached is not None:
        return cached

    items = db.query(models.InventoryItem).all()
    result = [schemas.InventoryOut.model_validate(i).model_dump() for i in items]
    cache.set_cached_inventory(result)
    return result


@app.post("/orders", response_model=schemas.OrderOut, status_code=201)
def create_order(
    order_in: schemas.OrderCreate,
    db: Session = Depends(get_db),
    current_customer: models.Customer = Depends(get_current_customer),
):
    """
    Creates an order atomically: validates inventory, decrements stock, and
    records the order in a single DB transaction. If anything fails partway
    through, the whole operation rolls back -- inventory is never left
    decremented without a corresponding order.

    Idempotent: replaying the same idempotency_key (e.g. after a client
    timeout/retry) returns the original order instead of double-processing.
    """
    existing_order = (
        db.query(models.Order)
        .filter(models.Order.idempotency_key == order_in.idempotency_key)
        .first()
    )
    if existing_order:
        # A replay only returns the original order to the customer who
        # created it. Keys are global (unique column), so if a different
        # customer sends the same key we reject it rather than leak someone
        # else's order.
        if existing_order.customer_id != current_customer.id:
            raise HTTPException(status_code=409, detail="Idempotency key already used")
        return existing_order

    try:
        # SELECT ... FOR UPDATE: lock this inventory row until commit, so two
        # concurrent orders for the same SKU can't both read the same
        # quantity and both decrement it (a lost update that oversells).
        item = (
            db.query(models.InventoryItem)
            .filter(models.InventoryItem.sku == order_in.sku)
            .with_for_update()
            .first()
        )
        if item is None:
            raise HTTPException(status_code=404, detail="SKU not found")
        if item.quantity_available < order_in.quantity:
            raise HTTPException(status_code=400, detail="Insufficient inventory")

        item.quantity_available -= order_in.quantity

        new_order = models.Order(
            idempotency_key=order_in.idempotency_key,
            customer_id=current_customer.id,
            sku=order_in.sku,
            quantity=order_in.quantity,
            total_amount=item.price * order_in.quantity,
            status=models.OrderStatus.PENDING,
        )
        db.add(new_order)
        db.commit()
        db.refresh(new_order)
        cache.invalidate_inventory_cache()
        return new_order
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Duplicate idempotency key")
    except Exception:
        db.rollback()
        raise


@app.get("/orders", response_model=List[schemas.OrderOut])
def list_orders(
    db: Session = Depends(get_db),
    current_customer: models.Customer = Depends(get_current_customer),
):
    # Customers see only their own orders; admins see everything.
    query = db.query(models.Order)
    if not current_customer.is_admin:
        query = query.filter(models.Order.customer_id == current_customer.id)
    return query.all()


@app.get("/orders/{order_id}", response_model=schemas.OrderOut)
def get_order(
    order_id: str,
    db: Session = Depends(get_db),
    current_customer: models.Customer = Depends(get_current_customer),
):
    order = db.get(models.Order, order_id)
    # 404 (not 403) for someone else's order, so order IDs can't be probed.
    if not order or (order.customer_id != current_customer.id and not current_customer.is_admin):
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@app.post("/orders/{order_id}/pay", response_model=schemas.OrderOut)
def pay_order(
    order_id: str,
    payment: schemas.PaymentRequest,
    db: Session = Depends(get_db),
    current_customer: models.Customer = Depends(get_current_customer),
):
    """
    Processes payment for a PENDING order. If the payment fails
    (simulate_failure=True stands in for a declined card / gateway error),
    this runs a compensating transaction: inventory is restored and the
    order is marked FAILED, atomically -- the system never ends up in a
    state where stock is gone but no payment and no valid order exist.
    """
    # Lock the order row so two concurrent /pay calls can't both see
    # PENDING (which would restore inventory twice on a failed payment).
    order = (
        db.query(models.Order)
        .filter(models.Order.id == order_id)
        .with_for_update()
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.customer_id != current_customer.id:
        raise HTTPException(status_code=403, detail="You can only pay for your own orders")
    if order.status != models.OrderStatus.PENDING:
        raise HTTPException(
            status_code=400, detail=f"Order is already {order.status.value}"
        )

    try:
        if payment.simulate_failure:
            item = (
                db.query(models.InventoryItem)
                .filter(models.InventoryItem.sku == order.sku)
                .with_for_update()
                .first()
            )
            item.quantity_available += order.quantity
            order.status = models.OrderStatus.FAILED
        else:
            order.status = models.OrderStatus.PAID

        db.commit()
        db.refresh(order)
        if payment.simulate_failure:
            cache.invalidate_inventory_cache()
        return order
    except Exception:
        db.rollback()
        raise


# Serve the built React dashboard as static files. Mounted last so it acts
# as a catch-all for any path not already matched by an API route above --
# API routes always take priority since Starlette matches routes in the
# order they were registered.
_frontend_dist = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
if os.path.isdir(_frontend_dist):
    app.mount("/", StaticFiles(directory=_frontend_dist, html=True), name="frontend")
