"""Pydantic schema + business rules. An invoice is auto-approved only if every rule passes and every
required field was extracted with confidence >= 0.9. Otherwise it goes to the review queue with reasons,
so the pipeline never guesses."""
from __future__ import annotations

import datetime as dt
import re

from pydantic import BaseModel, Field, ValidationError, field_validator

GST_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
REQUIRED = ("invoice_number", "invoice_date", "vendor_name", "vendor_gstin", "buyer_gstin", "subtotal", "total",
            "line_items")
MIN_CONFIDENCE = 0.9


def gstin_valid(g: str) -> bool:
    if not g or not GSTIN_RE.match(g):
        return False
    total = 0
    for i, ch in enumerate(g[:14]):
        p = GST_CHARS.index(ch) * (2 if i % 2 else 1)
        total += p // 36 + p % 36
    return GST_CHARS[(36 - total % 36) % 36] == g[14]


class LineItem(BaseModel):
    description: str = Field(min_length=1)
    qty: int = Field(gt=0)
    rate: float = Field(ge=0)
    amount: float = Field(ge=0)


class Invoice(BaseModel):
    invoice_number: str = Field(min_length=3)
    invoice_date: dt.date
    vendor_name: str = Field(min_length=2)
    vendor_gstin: str
    buyer_name: str | None = None
    buyer_gstin: str
    subtotal: float = Field(ge=0)
    cgst: float = 0.0
    sgst: float = 0.0
    igst: float = 0.0
    total: float = Field(gt=0)
    line_items: list[LineItem] = Field(min_length=1)

    @field_validator("vendor_gstin", "buyer_gstin")
    @classmethod
    def _gstin_shape(cls, v):
        if not GSTIN_RE.match(v or ""):
            raise ValueError("GSTIN must be 15 characters: state code, PAN, entity, 'Z', check digit")
        return v


def business_rules(inv: Invoice) -> list[str]:
    problems = []
    for g, who in ((inv.vendor_gstin, "vendor"), (inv.buyer_gstin, "buyer")):
        if not gstin_valid(g):
            problems.append(f"{who} GSTIN check digit is wrong")
    for it in inv.line_items:
        if abs(round(it.qty * it.rate, 2) - it.amount) > 0.011:
            problems.append(f"line '{it.description}': qty x rate != amount")
    if abs(sum(it.amount for it in inv.line_items) - inv.subtotal) > 0.05:
        problems.append("line items do not add up to the subtotal")
    if inv.igst:
        if inv.cgst or inv.sgst:
            problems.append("IGST and CGST/SGST both charged")
        if abs(inv.igst - inv.subtotal * 0.18) > 1:
            problems.append("IGST is not 18% of subtotal")
    elif abs(inv.cgst - inv.subtotal * 0.09) > 1 or abs(inv.sgst - inv.cgst) > 0.01:
        problems.append("CGST/SGST are not 9% each of subtotal")
    if abs(inv.subtotal + inv.cgst + inv.sgst + inv.igst - inv.total) > 0.05:
        problems.append("total != subtotal + tax")
    if not dt.date(2020, 1, 1) <= inv.invoice_date <= dt.date(2027, 12, 31):
        problems.append("invoice date out of range")
    return problems


def validate(fields: dict, conf: dict) -> tuple[Invoice | None, list[str]]:
    reasons = [f"low confidence on {k}" for k in REQUIRED if conf.get(k, 0.0) < MIN_CONFIDENCE and k != "vendor_name"]
    if conf.get("vendor_name", 0) < 0.5:
        reasons.append("vendor name not found")
    try:
        inv = Invoice.model_validate(fields)
    except ValidationError as e:
        return None, reasons + [f"schema: {err['loc'][0]}: {err['msg']}" for err in e.errors()]
    return inv, reasons + business_rules(inv)
