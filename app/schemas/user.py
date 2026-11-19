import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class SignupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: str
    phone: str = ""
    pin: str = Field(pattern=r"^[0-9]{4}$")
    role: Literal["customer"] = "customer"
    address_line1: str = Field(min_length=1)
    address_line2: str = ""
    city: str = Field(min_length=1)
    state: str = Field(min_length=1)
    pincode: str = Field(pattern=r"^[0-9]{6}$")

    @field_validator("name", "address_line1", "city", "state")
    @classmethod
    def non_empty_after_stripping(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("email")
    @classmethod
    def basic_email_format(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("must be a valid email address")
        return value


class SignupResponse(BaseModel):
    customer_id: str
    name: str
    message: str
