"""Email building tests. No database needed."""
from email import policy
from email.parser import BytesParser

import pytest

from app.emailer import FileBackend, build_message
from app.invoice import build_invoice_pdf
from tests.test_invoice import COMPANY, make_order

ARGS = dict(recipient="ayesha@example.com", sender="shop@example.com", company_name="Lahore Traders", currency="PKR")


def test_confirmation_email_has_summary_and_invoice_attachment():
    order = make_order(2)
    msg = build_message("order_confirmed", order=order, invoice_pdf=build_invoice_pdf(order, COMPANY), **ARGS)
    assert msg["To"] == "ayesha@example.com" and msg["From"] == "shop@example.com"
    assert msg["Subject"] == "Order #42 confirmed - Lahore Traders"
    body = msg.get_body(("plain",)).get_content()
    assert "Hi Ayesha Khan" in body and "INV-000042" in body and "PKR 4,938.00" in body
    assert "SKU-0001" in body
    [attachment] = list(msg.iter_attachments())
    assert attachment.get_filename() == "INV-000042.pdf" and attachment.get_content().startswith(b"%PDF")


@pytest.mark.parametrize("kind,word", [("order_shipped", "shipped"), ("order_completed", "complete"),
                                       ("order_cancelled", "cancelled")])
def test_status_emails(kind, word):
    msg = build_message(kind, order=make_order(status="cancelled"), **ARGS)
    assert word in msg["Subject"] and "#42" in msg["Subject"]
    assert not list(msg.iter_attachments())


def test_cancellation_mentions_the_reason():
    msg = build_message("order_cancelled", order=make_order(status="cancelled"), **ARGS)
    assert "Reason: Changed mind" in msg.get_content()


def test_low_stock_email():
    msg = build_message("low_stock", payload={"name": "Widget", "sku": "W-1", "stock": 3, "threshold": 5},
                        **{**ARGS, "recipient": "boss@example.com"})
    assert msg["Subject"] == "Low stock: Widget (W-1)" and "down to 3" in msg.get_content()


def test_unknown_kind_is_an_error():
    with pytest.raises(ValueError):
        build_message("nope", order=make_order(), **ARGS)


def test_file_backend_writes_a_readable_eml(tmp_path):
    order = make_order()
    msg = build_message("order_confirmed", order=order, invoice_pdf=build_invoice_pdf(order, COMPANY), **ARGS)
    path = FileBackend(tmp_path / "outbox").send(msg, "n7-order_confirmed")
    parsed = BytesParser(policy=policy.default).parsebytes(open(path, "rb").read())
    assert parsed["Subject"] == msg["Subject"]
    assert [a.get_filename() for a in parsed.iter_attachments()] == ["INV-000042.pdf"]
