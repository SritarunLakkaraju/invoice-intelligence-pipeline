import datetime as dt
import os
import random

import pytest
from fastapi.testclient import TestClient

from fde4_doc_pipeline.evaluate import _ensure_data
from fde4_doc_pipeline.extract import OfflineInvoiceExtractor, parse_date, pdf_text
from fde4_doc_pipeline.generate_invoices import OUT, gstin_checksum, make_gstin
from fde4_doc_pipeline.infra import PubSub
from fde4_doc_pipeline.pipeline import Pipeline
from fde4_doc_pipeline.validate import Invoice, business_rules, gstin_valid, validate

_ensure_data()
FILES = sorted(os.listdir(OUT))


def test_gstin_checksum_round_trip():
    rng = random.Random(1)
    for _ in range(50):
        g = make_gstin(rng)
        assert gstin_valid(g)
        bad = g[:-1] + ("A" if g[-1] != "A" else "B")
        assert not gstin_valid(bad)
    assert gstin_checksum("27AAPFU0939F1Z") == "V"  # a widely published example GSTIN: 27AAPFU0939F1ZV


def test_date_formats():
    assert parse_date("14/09/2026") == parse_date("2026-09-14") == parse_date("14-Sep-2026") == "2026-09-14"
    assert parse_date("not a date") is None


@pytest.mark.parametrize("fname", FILES[:6])
def test_extraction_finds_required_fields(fname):
    f, conf = OfflineInvoiceExtractor().extract(pdf_text(os.path.join(OUT, fname)))
    assert f["invoice_number"] and f["vendor_gstin"] and f["total"] > 0 and f["line_items"]


def _good_invoice(**over):
    g1, g2 = make_gstin(random.Random(2)), make_gstin(random.Random(3))
    d = dict(invoice_number="ABC/1", invoice_date=dt.date(2026, 9, 1), vendor_name="Vendor Co", vendor_gstin=g1,
             buyer_gstin=g2, subtotal=1000.0, cgst=90.0, sgst=90.0, total=1180.0,
             line_items=[{"description": "x", "qty": 2, "rate": 500.0, "amount": 1000.0}])
    d.update(over)
    return Invoice.model_validate(d)


def test_business_rules():
    assert business_rules(_good_invoice()) == []
    assert "total != subtotal + tax" in business_rules(_good_invoice(total=1200.0))
    assert any("qty x rate" in p for p in business_rules(_good_invoice(
        line_items=[{"description": "x", "qty": 3, "rate": 500.0, "amount": 1000.0}])))
    assert any("IGST" in p for p in business_rules(_good_invoice(igst=50.0)))


def test_validate_routes_missing_fields_to_review():
    inv, reasons = validate({"invoice_number": "X"}, {"invoice_number": 1.0})
    assert inv is None and any("schema" in r for r in reasons)


def test_pubsub_retries_then_dead_letters():
    bus = PubSub(max_attempts=3)
    bus.publish({"x": 1})
    calls = []

    def boom(env):
        calls.append(env)
        raise RuntimeError("down")

    bus.drain(boom)
    assert len(calls) == 3 and len(bus.dead_letter) == 1


def test_pipeline_end_to_end_and_idempotent(tmp_path):
    p = Pipeline(str(tmp_path))
    for f in FILES[:20]:
        p.upload(os.path.join(OUT, f))
    p.run()
    total = p.store.count("invoices") + p.store.count("review_queue")
    assert total == 20
    p.upload(os.path.join(OUT, FILES[0]))
    p.run()
    assert p.store.count("invoices") + p.store.count("review_queue") == 20
    # the re-delivered file was recognised by its SHA-256 and skipped (no new row, no new result entry)
    assert len(p.worker.results) == 20


def test_evaluation_quality_bar():
    import contextlib
    import io

    from fde4_doc_pipeline.evaluate import evaluate

    with contextlib.redirect_stdout(io.StringIO()):
        m = evaluate(verbose=False)
    assert m["overall_field_accuracy"] >= 0.98
    assert m["review_recall"] == 1.0 and m["unseen_layout"]["auto_approved"] == 0
    assert m["idempotency"]["rows_before"] == m["idempotency"]["rows_after_reupload_30"]


def test_api_upload_review_and_approve(tmp_path, monkeypatch):
    import importlib
    import json

    monkeypatch.setenv("FDE4_DATA_DIR", str(tmp_path))
    api = importlib.reload(importlib.import_module("fde4_doc_pipeline.api"))
    truth = {t["file"]: t for t in json.load(open(os.path.join(OUT, "..", "truth.json"), encoding="utf-8"))}
    bad = next(f for f, t in truth.items() if t["problem"] == "total_mismatch")
    good = next(f for f, t in truth.items() if not t["should_review"])
    with TestClient(api.app) as c:
        for f in (good, bad):
            with open(os.path.join(OUT, f), "rb") as fh:
                r = c.post("/upload", files={"file": (f, fh, "application/pdf")})
            assert r.status_code == 200
        assert r.json()["route"] == "review"
        assert c.post("/upload", files={"file": ("x.txt", b"hi", "text/plain")}).status_code == 415
        assert bad in c.get("/review").text
        doc = r.json()["doc_id"]
        # a human fixes the total to the correct value and approves
        fixed = c.post(f"/review/{doc}", json={"action": "approve", "total": truth[bad]["total"]})
        assert fixed.status_code == 200 and fixed.json()["status"] == "approved"
        assert len(c.get("/invoices").json()) == 2
    api.pipe.cache_clear()
