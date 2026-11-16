"""FDE-4 Cloud Run service. Locally: uvicorn fde4_doc_pipeline.api:app --port 8004

POST /pubsub/push        Pub/Sub push endpoint (real envelope format). 2xx = ack, 5xx = nack -> Pub/Sub retries.
POST /upload             upload a PDF (stands in for a GCS upload); it is published and processed immediately
GET  /review             HTML review queue: low-confidence / rule-breaking invoices with the reasons
POST /review/{doc_id}    approve with corrections (form or JSON) or reject
GET  /invoices           approved rows (what lands in BigQuery)
"""
from __future__ import annotations

import html
import json
import os
import tempfile
from functools import lru_cache

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from .pipeline import Pipeline
from .validate import business_rules, Invoice

app = FastAPI(title="Invoice Intelligence Pipeline (FDE-4)", version="1.0")


@lru_cache(maxsize=1)
def pipe() -> Pipeline:
    root = os.environ.get("FDE4_DATA_DIR") or tempfile.mkdtemp(prefix="fde4_")
    os.makedirs(root, exist_ok=True)
    return Pipeline(root, db=os.path.join(root, "warehouse.sqlite"))


@app.post("/pubsub/push")
async def pubsub_push(req: Request):
    env = await req.json()
    try:
        res = pipe().worker.handle_push(env)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))  # 4xx: retrying will not help; Pub/Sub dead-letters it eventually
    return {"route": res["route"], "reasons": res["reasons"]}


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(415, "PDF files only")
    p = pipe()
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(await file.read())
    p.bucket.upload(tmp.name, os.path.basename(file.filename))  # fires the finalize event -> Pub/Sub
    os.unlink(tmp.name)
    p.run()
    return p.worker.results[-1] if p.worker.results else {"route": "duplicate"}


@app.get("/invoices")
def invoices():
    return pipe().store.invoices()


@app.get("/review", response_class=HTMLResponse)
def review_page():
    rows = pipe().store.open_reviews()
    cards = []
    for r in rows:
        f = r["fields"]
        inputs = "".join(
            f"<label>{k}<input name='{k}' value='{html.escape(str(f.get(k) if f.get(k) is not None else ''))}'></label>"
            for k in ("invoice_number", "invoice_date", "vendor_name", "vendor_gstin", "buyer_gstin", "subtotal",
                      "cgst", "sgst", "igst", "total"))
        cards.append(f"<form class=card method=post action='/review/{r['doc_id']}'><h3>{html.escape(r['object_name'])}</h3>"
                     f"<p class=why>{'<br>'.join(html.escape(x) for x in r['reasons'])}</p>{inputs}"
                     f"<button name=action value=approve>Approve</button> "
                     f"<button name=action value=reject class=rej>Reject</button></form>")
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Invoice review queue</title><style>body{font-family:system-ui;max-width:820px;margin:30px auto;"
            "padding:0 16px}.card{border:1px solid #ddd;border-radius:10px;padding:14px;margin:14px 0}"
            "label{display:inline-block;margin:4px 8px 4px 0;font-size:13px}input{display:block;padding:5px;width:200px}"
            ".why{color:#b45309}button{padding:8px 14px;background:#15803d;color:#fff;border:0;border-radius:6px}"
            ".rej{background:#b91c1c}</style>"
            f"<h2>Review queue ({len(rows)} open)</h2>" + ("".join(cards) or "<p>Nothing to review.</p>"))


@app.post("/review/{doc_id}")
async def review_action(doc_id: str, req: Request):
    p = pipe()
    item = next((r for r in p.store.open_reviews() if r["doc_id"] == doc_id), None)
    if not item:
        raise HTTPException(404, "no open review item with that id")
    body = await req.json() if req.headers.get("content-type", "").startswith("application/json") \
        else dict(await req.form())
    if body.get("action") == "reject":
        p.store.resolve_review(doc_id, "rejected")
        return {"status": "rejected"}
    fields = dict(item["fields"])
    for k, v in body.items():
        if k != "action" and v != "":
            fields[k] = float(v) if k in ("subtotal", "cgst", "sgst", "igst", "total") else v
    try:
        inv = Invoice.model_validate(fields)
    except Exception as e:
        raise HTTPException(422, f"corrected invoice is still invalid: {e}")
    problems = [x for x in business_rules(inv) if "GSTIN" not in x or body.get("override_gstin") != "yes"]
    if problems and body.get("force") != "yes":
        raise HTTPException(422, {"problems": problems, "hint": "fix the fields or send force=yes to accept anyway"})
    p.store.insert_invoice(doc_id, item["object_name"], inv.model_dump(), source="human")
    p.store.resolve_review(doc_id, "approved")
    if "application/json" in req.headers.get("content-type", ""):
        return {"status": "approved", "invoice": json.loads(inv.model_dump_json())}
    return RedirectResponse("/review", status_code=303)
