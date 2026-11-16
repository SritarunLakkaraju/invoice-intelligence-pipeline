"""Generates 120 labelled GST invoice PDFs (3 different supplier layouts) plus ground truth.

Some invoices carry deliberate problems that the pipeline must NOT auto-approve:
  * arithmetic errors (the printed grand total does not equal subtotal + tax)
  * an invalid GSTIN (bad checksum)
truth.json records every field and whether the invoice should go to human review.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "invoices")
UNSEEN = os.path.join(HERE, "data", "invoices_unseen_layout")
GST_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

VENDORS = ["Sunrise Office Supplies Pvt Ltd", "Deccan Electricals", "Kaveri Packaging Co", "Malabar IT Services LLP",
           "Indus Logistics Pvt Ltd", "Godavari Print House", "Nilgiri Furniture Works", "Konark Cloud Solutions"]
BUYERS = ["Northbridge Institute of Technology", "Acme Logistics India Pvt Ltd", "Bluepeak Foods Ltd"]
ITEMS = [("A4 paper ream", 280), ("Laser toner cartridge", 3450), ("LED panel light", 1250), ("Office chair", 6800),
         ("Corrugated boxes (pack of 50)", 1900), ("Network switch 24-port", 15800), ("Annual cloud hosting", 42000),
         ("Printed brochures (500)", 7200), ("Freight charges", 5600), ("Whiteboard 4x3 ft", 3100)]


def gstin_checksum(first14: str) -> str:
    """Official GSTIN check digit: weighted base-36 sum (weights 1,2,1,2,...), digits of each product summed."""
    total = 0
    for i, ch in enumerate(first14):
        p = GST_CHARS.index(ch) * (2 if i % 2 else 1)
        total += p // 36 + p % 36
    return GST_CHARS[(36 - total % 36) % 36]


def make_gstin(rng) -> str:
    state = f"{rng.choice([27, 29, 32, 33, 36, 7, 9]):02d}"
    pan = "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(5)) + f"{rng.randint(0, 9999):04d}" + \
        rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    first14 = state + pan + str(rng.randint(1, 9)) + "Z"
    return first14 + gstin_checksum(first14)


def _draw(c: canvas.Canvas, layout: str, inv: dict):
    W, H = A4
    y = H - 60
    if layout == "A":
        c.setFont("Helvetica-Bold", 16); c.drawString(50, y, "TAX INVOICE"); y -= 30
        c.setFont("Helvetica", 10)
        for line in [f"Seller: {inv['vendor_name']}", f"GSTIN: {inv['vendor_gstin']}",
                     f"Invoice No: {inv['invoice_number']}", f"Invoice Date: {inv['invoice_date_fmt']}",
                     f"Bill To: {inv['buyer_name']}", f"Buyer GSTIN: {inv['buyer_gstin']}"]:
            c.drawString(50, y, line); y -= 16
    elif layout == "B":
        c.setFont("Helvetica-Bold", 14); c.drawString(50, y, inv["vendor_name"].upper()); y -= 18
        c.setFont("Helvetica", 10)
        c.drawString(50, y, f"Supplier GSTIN {inv['vendor_gstin']}"); y -= 26
        c.setFont("Helvetica-Bold", 12); c.drawString(50, y, "INVOICE"); y -= 20
        c.setFont("Helvetica", 10)
        for line in [f"Bill Number: {inv['invoice_number']}", f"Dated {inv['invoice_date_fmt']}",
                     f"Customer: {inv['buyer_name']}", f"Customer GSTIN {inv['buyer_gstin']}"]:
            c.drawString(50, y, line); y -= 16
    elif layout == "D":  # a supplier format the rules were never written for
        c.setFont("Helvetica-Bold", 13); c.drawString(50, y, f"From {inv['vendor_name']}"); y -= 18
        c.setFont("Helvetica", 10)
        for line in [f"Registration {inv['vendor_gstin']}", f"Document Ref {inv['invoice_number']}",
                     f"Issued on {inv['invoice_date_fmt']}", f"To {inv['buyer_name']} / {inv['buyer_gstin']}"]:
            c.drawString(50, y, line); y -= 16
    else:  # C: labels on the right-hand side
        c.setFont("Helvetica-Bold", 15); c.drawString(50, y, inv["vendor_name"])
        c.setFont("Helvetica", 9); c.drawRightString(W - 50, y, f"Inv # {inv['invoice_number']}"); y -= 14
        c.drawRightString(W - 50, y, f"Date: {inv['invoice_date_fmt']}"); y -= 14
        c.drawString(50, y, f"GSTIN/UIN: {inv['vendor_gstin']}"); y -= 24
        c.setFont("Helvetica", 10)
        c.drawString(50, y, f"Billed to: {inv['buyer_name']} (GSTIN/UIN: {inv['buyer_gstin']})"); y -= 20
    y -= 10
    c.setFont("Helvetica-Bold", 10)
    for x, h in ((50, "Description"), (300, "Qty"), (360, "Rate"), (460, "Amount")):
        c.drawString(x, y, h)
    y -= 16
    c.setFont("Helvetica", 10)
    for it in inv["items"]:
        c.drawString(50, y, it["description"]); c.drawString(300, y, str(it["qty"]))
        c.drawString(360, y, f"{it['rate']:,.2f}"); c.drawString(460, y, f"{it['amount']:,.2f}"); y -= 15
    y -= 10
    sub_label, total_label = {"A": ("Sub Total", "Grand Total"), "B": ("Taxable Value", "Amount Payable"),
                              "C": ("Subtotal", "Total Due"), "D": ("Net Amount", "Total (INR)")}[layout]
    lines = [(sub_label, inv["subtotal"])]
    if inv["igst"]:
        lines.append(("IGST @18%", inv["igst"]))
    else:
        lines += [("CGST @9%", inv["cgst"]), ("SGST @9%", inv["sgst"])]
    lines.append((total_label, inv["printed_total"]))
    for label, v in lines:
        c.drawString(330, y, label); c.drawRightString(540, y, f"Rs. {v:,.2f}"); y -= 15


def generate(n: int = 120, seed: int = 5, out: str = OUT, layout_override: str | None = None,
             truth_name: str = "truth.json") -> list[dict]:
    """layout_override='D' produces invoices from an unseen supplier format (robustness test)."""
    rng = random.Random(seed)
    os.makedirs(out, exist_ok=True)
    for f in os.listdir(out):
        os.remove(os.path.join(out, f))
    vendors = {v: (make_gstin(rng), rng.choice("ABC")) for v in VENDORS}
    truth = []
    bad_total = set(rng.sample(range(n), n // 15))  # 8 of 120
    bad_gstin = set(rng.sample(sorted(set(range(n)) - bad_total), n // 30))  # 4 of 120
    for i in range(n):
        vname = rng.choice(VENDORS)
        vg, layout = vendors[vname]
        layout = layout_override or layout
        if i in bad_gstin:
            vg = vg[:-1] + GST_CHARS[(GST_CHARS.index(vg[-1]) + 7) % 36]  # corrupt the check digit
        date = dt.date(2026, 9, 1) + dt.timedelta(days=rng.randint(0, 60))
        fmt = {"A": date.strftime("%d/%m/%Y"), "B": date.isoformat(), "C": date.strftime("%d-%b-%Y"),
               "D": date.strftime("%d %B %Y")}[layout]
        items = []
        for desc, price in rng.sample(ITEMS, rng.randint(1, 4)):
            qty = rng.randint(1, 20)
            rate = round(price * rng.uniform(0.9, 1.1), 2)
            items.append({"description": desc, "qty": qty, "rate": rate, "amount": round(qty * rate, 2)})
        sub = round(sum(it["amount"] for it in items), 2)
        interstate = rng.random() < 0.3
        igst = round(sub * 0.18, 2) if interstate else 0.0
        cgst = sgst = 0.0 if interstate else round(sub * 0.09, 2)
        total = round(sub + igst + cgst + sgst, 2)
        printed = round(total + rng.choice([100, 1000, -250, 90]), 2) if i in bad_total else total
        inv = {"file": f"invoice_{i:03d}.pdf", "layout": layout, "vendor_name": vname, "vendor_gstin": vg,
               "buyer_name": rng.choice(BUYERS), "buyer_gstin": make_gstin(rng),
               "invoice_number": f"{vname.split()[0][:3].upper()}/{2026}/{rng.randint(100, 9999):04d}",
               "invoice_date": date.isoformat(), "invoice_date_fmt": fmt, "items": items, "subtotal": sub,
               "cgst": cgst, "sgst": sgst, "igst": igst, "total": total, "printed_total": printed,
               "should_review": i in bad_total or i in bad_gstin,
               "problem": "total_mismatch" if i in bad_total else "bad_gstin" if i in bad_gstin else None}
        c = canvas.Canvas(os.path.join(out, inv["file"]), pagesize=A4)
        _draw(c, layout, inv)
        c.save()
        truth.append(inv)
    with open(os.path.join(HERE, "data", truth_name), "w", encoding="utf-8") as f:
        json.dump(truth, f, indent=1)
    return truth


if __name__ == "__main__":
    t = generate()
    print(f"wrote {len(t)} invoices to {OUT}; {sum(x['should_review'] for x in t)} have planted problems")
    u = generate(n=10, seed=99, out=UNSEEN, layout_override="D", truth_name="truth_unseen.json")
    print(f"wrote {len(u)} unseen-layout invoices to {UNSEEN}")
