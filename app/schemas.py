from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, ConfigDict

from .models import OrderStatus


class CustomerCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, description="At least 8 characters")


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: str
    is_admin: bool


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class InventoryCreate(BaseModel):
    sku: str
    name: str
    price: float
    quantity_available: int


class InventoryOut(InventoryCreate):
    model_config = ConfigDict(from_attributes=True)


class OrderCreate(BaseModel):
    # No customer_id here on purpose -- who's ordering is derived from the
    # caller's JWT, never taken from the request body. See main.py.
    idempotency_key: str
    sku: str
    quantity: int = Field(gt=0)


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    idempotency_key: str
    customer_id: str
    sku: str
    quantity: int
    total_amount: float
    status: OrderStatus
    created_at: datetime


class PaymentRequest(BaseModel):
    simulate_failure: bool = False
