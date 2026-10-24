"""PDF invoice tests. No database needed."""
import re
from datetime import datetime, timezone

from app.invoice import build_invoice_pdf, text_width

COMPANY = {"name": "Lahore Traders", "address_lines": ["12 Mall Road", "Lahore"], "currency": "PKR"}


def make_order(n_items=3, status="confirmed", name="Ayesha Khan"):
    items = [{"product_id": i, "sku": f"SKU-{i:04d}",
              "name": f"Product {i} with a fairly long descriptive name that needs wrapping across lines",
              "unit_price": "1234.50", "quantity": 2, "line_total": "2469.00"} for i in range(1, n_items + 1)]
    return {"id": 42, "invoice_number": "INV-000042", "status": status,
            "created_at": datetime(2026, 10, 12, 10, 30, tzinfo=timezone.utc),
            "cancel_reason": "Changed mind" if status == "cancelled" else None,
            "customer": {"name": name, "email": "ayesha@example.com", "phone": "+92 321 1234567",
                         "address": "House 5, Street 7, Gulberg III, Lahore"},
            "items": items, "total": f"{2469 * n_items:.2f}"}


def pdf_text(pdf: bytes) -> str:
    """All text shown on the pages (octal escapes decoded)."""
    out = []
    for raw in re.findall(rb"\(((?:\\.|[^\\)])*)\) Tj", pdf):
        out.append(re.sub(rb"\\([0-7]{3}|.)",
                          lambda m: bytes([int(m.group(1), 8)]) if len(m.group(1)) == 3 else m.group(1), raw)
                   .decode("cp1252"))
    return "\n".join(out)


def assert_valid_structure(pdf: bytes):
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF")
    xref_pos = int(re.search(rb"startxref\n(\d+)\n%%EOF", pdf).group(1))
    assert pdf[xref_pos:xref_pos + 4] == b"xref"
    size = int(re.search(rb"xref\n0 (\d+)\n", pdf).group(1))
    table = pdf[xref_pos:].split(b"\n")[2:2 + size]
    for number, entry in enumerate(table):
        if number == 0:
            continue
        offset = int(entry[:10])
        assert pdf[offset:].startswith(b"%d 0 obj" % number), f"bad xref offset for object {number}"
    for m in re.finditer(rb"/Length (\d+) >>\nstream\n", pdf):
        body_start = m.end()
        assert pdf[body_start + int(m.group(1)):].startswith(b"\nendstream")


def test_invoice_is_a_valid_pdf_with_the_right_content():
    pdf = build_invoice_pdf(make_order(), COMPANY)
    assert_valid_structure(pdf)
    text = pdf_text(pdf)
    for expected in ("Lahore Traders", "INVOICE", "INV-000042", "12 Oct 2026", "Ayesha Khan",
                     "ayesha@example.com", "SKU-0001", "1,234.50", "2,469.00", "PKR 7,407.00",
                     "Status: Confirmed", "Page 1 of 1"):
        assert expected in text, expected
    assert "CANCELLED" not in text


def test_long_orders_paginate_and_repeat_the_header():
    pdf = build_invoice_pdf(make_order(60), COMPANY)
    assert_valid_structure(pdf)
    pages = len(re.findall(rb"/Type /Page /Parent", pdf))
    assert pages >= 3
    text = pdf_text(pdf)
    assert f"Page {pages} of {pages}" in text and text.count("(continued)") == pages - 1
    assert text.count("PKR 148,140.00") == 1          # the grand total appears once
    assert "SKU-0060" in text


def test_cancelled_invoice_is_stamped():
    text = pdf_text(build_invoice_pdf(make_order(status="cancelled"), COMPANY))
    assert "CANCELLED" in text and "Reason: Changed mind" in text


def test_awkward_characters_do_not_break_the_pdf():
    order = make_order(name="محمد (Test) \\ Ünïcode")
    order["items"][0]["name"] = 'Widget (large) \\ 5" - special'
    pdf = build_invoice_pdf(order, COMPANY)
    assert_valid_structure(pdf)
    assert "(Test)" in pdf_text(pdf) and "Widget (large)" in pdf_text(pdf)


def test_orders_without_customer_still_render():
    order = make_order()
    order["customer"] = None
    assert_valid_structure(build_invoice_pdf(order, COMPANY))


def test_same_order_gives_identical_bytes():
    assert build_invoice_pdf(make_order(), COMPANY) == build_invoice_pdf(make_order(), COMPANY)


def test_text_width_is_sane():
    assert abs(text_width("0123456789", 10) - 55.6) < 0.01
    assert text_width("W", 10, bold=True) > text_width("i", 10, bold=True)
