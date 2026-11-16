"""PDF -> text -> invoice fields (with per-field confidence).

OfflineInvoiceExtractor: label-based rules that cover the three supplier layouts (labels differ: "Invoice No",
"Bill Number", "Inv #"...). Every field records whether it was found by an exact label (confidence 1.0) or a
weaker fallback (0.6). Missing fields get 0. GeminiInvoiceExtractor sends the PDF text to Gemini with a strict
JSON schema; Document AI's Invoice Parser is the managed alternative on GCP (same output contract).
"""
from __future__ import annotations

import datetime as dt
import re

from pypdf import PdfReader

AMOUNT = r"([\d,]+\.\d{2})"


def pdf_text(path: str) -> str:
    return "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def parse_date(s: str) -> str | None:
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y", "%d.%m.%Y", "%d %B %Y"):
        try:
            return dt.datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


class OfflineInvoiceExtractor:
    name = "offline-rules"
    LABELS = {
        "invoice_number": [r"Invoice No[:.]?\s*(\S+)", r"Bill Number[:.]?\s*(\S+)", r"Inv #\s*(\S+)"],
        "invoice_date": [r"Invoice Date[:.]?\s*([\d/\-A-Za-z]+)", r"Dated\s+([\d/\-A-Za-z]+)", r"\bDate[:.]?\s*([\d/\-A-Za-z]+)"],
        "vendor_gstin": [r"(?:^|\n)GSTIN[:.]?\s*([0-9A-Z]{15})", r"Supplier GSTIN[:.]?\s*([0-9A-Z]{15})",
                         r"(?:^|\n)GSTIN/UIN[:.]?\s*([0-9A-Z]{15})"],
        "buyer_gstin": [r"Buyer GSTIN[:.]?\s*([0-9A-Z]{15})", r"Customer GSTIN[:.]?\s*([0-9A-Z]{15})",
                        r"Billed to: .*?\(GSTIN/UIN: ([0-9A-Z]{15})\)"],
        "buyer_name": [r"Bill To[:.]?\s*(.+)", r"Customer[:.]\s*(.+)", r"Billed to[:.]?\s*(.+?) \(GSTIN"],
        "subtotal": [rf"(?:Sub Total|Subtotal|Taxable Value)\s*Rs\.\s*{AMOUNT}"],
        "total": [rf"(?:Grand Total|Amount Payable|Total Due)\s*Rs\.\s*{AMOUNT}"],
        "cgst": [rf"CGST @\s*\d+%\s*Rs\.\s*{AMOUNT}"],
        "sgst": [rf"SGST @\s*\d+%\s*Rs\.\s*{AMOUNT}"],
        "igst": [rf"IGST @\s*\d+%\s*Rs\.\s*{AMOUNT}"],
    }

    def extract(self, text: str) -> tuple[dict, dict]:
        flat = re.sub(r"[ \t]+", " ", text)
        joined = re.sub(r"\n(?=Rs\.)", " ", flat)  # a label and its amount sit on consecutive lines
        out, conf = {}, {}
        for field, pats in self.LABELS.items():
            for p in pats:
                if m := re.search(p, joined):
                    out[field] = m.group(1).strip()
                    conf[field] = 1.0
                    break
        # vendor name: explicit label, else the first line of the document (letterhead)
        lines = [ln.strip() for ln in flat.splitlines() if ln.strip()]
        if m := re.search(r"Seller[:.]?\s*(.+)", flat):
            out["vendor_name"], conf["vendor_name"] = m.group(1).strip(), 1.0
        elif lines:
            out["vendor_name"], conf["vendor_name"] = lines[0], 0.6
        if "invoice_date" in out:
            iso = parse_date(out["invoice_date"])
            out["invoice_date"], conf["invoice_date"] = iso, 1.0 if iso else 0.0
        for k in ("subtotal", "total", "cgst", "sgst", "igst"):
            if k in out:
                out[k] = _num(out[k])
        out.setdefault("cgst", 0.0)
        out.setdefault("sgst", 0.0)
        out.setdefault("igst", 0.0)
        out["line_items"] = self._items(lines)
        conf["line_items"] = 1.0 if out["line_items"] else 0.0
        return out, conf

    @staticmethod
    def _items(lines: list[str]) -> list[dict]:
        """Rows come out of the PDF as 4 consecutive lines: description, qty, rate, amount."""
        try:
            start = lines.index("Amount") + 1
        except ValueError:
            return []
        items, i = [], start
        while i + 3 < len(lines) and re.fullmatch(r"\d+", lines[i + 1]) and re.fullmatch(AMOUNT, lines[i + 2]):
            items.append({"description": lines[i], "qty": int(lines[i + 1]), "rate": _num(lines[i + 2]),
                          "amount": _num(lines[i + 3])})
            i += 4
        return items


class GeminiInvoiceExtractor:
    name = "gemini"
    PROMPT = ("Extract this GST invoice as JSON with keys invoice_number, invoice_date (YYYY-MM-DD), vendor_name, "
              "vendor_gstin, buyer_name, buyer_gstin, subtotal, cgst, sgst, igst, total (numbers, 0 if absent), "
              "line_items (list of description, qty, rate, amount). Copy values exactly; use null if a value is "
              "not present. Invoice text:\n{text}")

    def __init__(self):
        from common.llm import GeminiLLM

        self.llm = GeminiLLM()

    def extract(self, text: str) -> tuple[dict, dict]:
        data = self.llm.generate_json(self.PROMPT.format(text=text))
        conf = {k: (0.9 if data.get(k) not in (None, "", []) else 0.0) for k in
                ("invoice_number", "invoice_date", "vendor_name", "vendor_gstin", "buyer_name", "buyer_gstin",
                 "subtotal", "total", "line_items")}
        return data, conf
