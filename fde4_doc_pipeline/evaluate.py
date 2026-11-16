"""FDE-4 evaluation (python -m fde4_doc_pipeline.evaluate). Writes results/metrics.json.

* field-level accuracy on 120 labelled invoices (each field compared with ground truth)
* routing: % fully automated, and whether every planted problem was caught (recall) without flagging
  clean invoices (precision)
* unseen supplier layout: none of those invoices may be auto-approved
* idempotency: re-uploading every file creates no new rows; a poison message ends in the dead-letter queue
* seconds per document and an estimated compute cost per 1,000 documents on Cloud Run
"""
from __future__ import annotations

import json
import os
import statistics
import tempfile

from .generate_invoices import OUT, UNSEEN, generate
from .pipeline import run_folder

HERE = os.path.dirname(os.path.abspath(__file__))
FIELDS = ["invoice_number", "invoice_date", "vendor_name", "vendor_gstin", "buyer_gstin", "subtotal", "tax", "total",
          "line_items"]
# Cloud Run list prices (USD, tier 1, request-based billing) - check the current price list before quoting.
VCPU_SECOND, GIB_SECOND = 0.000024, 0.0000025


def _ensure_data():
    if not (os.path.isdir(OUT) and os.listdir(OUT)):
        generate()
    if not (os.path.isdir(UNSEEN) and os.listdir(UNSEEN)):
        generate(n=10, seed=99, out=UNSEEN, layout_override="D", truth_name="truth_unseen.json")


def field_matches(f: dict, t: dict) -> dict[str, bool]:
    tax = round(f.get("cgst", 0) + f.get("sgst", 0) + f.get("igst", 0), 2)
    items = f.get("line_items") or []
    return {
        "invoice_number": f.get("invoice_number") == t["invoice_number"],
        "invoice_date": f.get("invoice_date") == t["invoice_date"],
        "vendor_name": (f.get("vendor_name") or "").lower() == t["vendor_name"].lower(),
        "vendor_gstin": f.get("vendor_gstin") == t["vendor_gstin"],
        "buyer_gstin": f.get("buyer_gstin") == t["buyer_gstin"],
        "subtotal": abs((f.get("subtotal") or -1) - t["subtotal"]) < 0.01,
        "tax": abs(tax - round(t["cgst"] + t["sgst"] + t["igst"], 2)) < 0.01,
        "total": abs((f.get("total") or -1) - t["printed_total"]) < 0.01,
        "line_items": len(items) == len(t["items"]) and all(
            abs(a["amount"] - b["amount"]) < 0.01 and a["qty"] == b["qty"] for a, b in zip(items, t["items"])),
    }


def evaluate(verbose=True) -> dict:
    _ensure_data()
    with open(os.path.join(HERE, "data", "truth.json"), encoding="utf-8") as fh:
        truth = {t["file"]: t for t in json.load(fh)}
    with tempfile.TemporaryDirectory() as tmp:
        p = run_folder(OUT, tmp)
        res = {r["object_name"]: r for r in p.worker.results}
        per_field = {k: 0 for k in FIELDS}
        for name, t in truth.items():
            for k, ok in field_matches(res[name]["fields"], t).items():
                per_field[k] += ok
        n = len(truth)
        tp = sum(res[k]["route"] == "review" and t["should_review"] for k, t in truth.items())
        fp = sum(res[k]["route"] == "review" and not t["should_review"] for k, t in truth.items())
        fn = sum(res[k]["route"] == "auto" and t["should_review"] for k, t in truth.items())
        ms = [r["ms"] for r in p.worker.results]

        rows_before = p.store.count("invoices") + p.store.count("review_queue")
        for fname in sorted(os.listdir(OUT))[:30]:  # re-upload (and so re-deliver) 30 files
            p.upload(os.path.join(OUT, fname))
        p.run()
        rows_after = p.store.count("invoices") + p.store.count("review_queue")
        p.bus.publish({"bucket": "invoices-inbox", "name": "does_not_exist.pdf"})  # poison message
        p.run()
        dead = len(p.bus.dead_letter)
        attempts = p.bus.dead_letter[0].attempts if dead else 0

    with tempfile.TemporaryDirectory() as tmp:
        u = run_folder(UNSEEN, tmp)
        unseen_auto = u.store.count("invoices")
        unseen_total = len(u.worker.results)

    sec = statistics.mean(ms) / 1000
    cost_1k = 1000 * sec * (VCPU_SECOND + 0.5 * GIB_SECOND)
    out = {"documents": n, "field_accuracy": {k: round(v / n, 4) for k, v in per_field.items()},
           "overall_field_accuracy": round(sum(per_field.values()) / (n * len(FIELDS)), 4),
           "automated_pct": round(100 * sum(r["route"] == "auto" for r in res.values()) / n, 1),
           "planted_problems": sum(t["should_review"] for t in truth.values()),
           "review_recall": round(tp / (tp + fn), 4) if tp + fn else None,
           "review_precision": round(tp / (tp + fp), 4) if tp + fp else None,
           "unseen_layout": {"documents": unseen_total, "auto_approved": unseen_auto},
           "idempotency": {"rows_before": rows_before, "rows_after_reupload_30": rows_after},
           "poison_message": {"dead_lettered": dead, "attempts": attempts},
           "ms_per_doc": {"mean": round(statistics.mean(ms), 2), "p95": round(sorted(ms)[int(0.95 * (len(ms) - 1))], 2)},
           "est_cloud_run_compute_usd_per_1000_docs": round(cost_1k, 6)}
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    with open(os.path.join(HERE, "results", "metrics.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    if verbose:
        print(json.dumps(out, indent=2))
    return out


if __name__ == "__main__":
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):  # silence per-document log lines
        m = evaluate(verbose=False)
    print(json.dumps(m, indent=2))
