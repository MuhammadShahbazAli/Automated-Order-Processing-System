"""Daily report email rendering. No database needed."""
from zoneinfo import ZoneInfo

from app.emailer import build_message, render_report

KARACHI = ZoneInfo("Asia/Karachi")


def make_report(**overrides):
    report = {
        "period": {"start": "2026-10-11T03:00:00+00:00", "end": "2026-10-12T03:00:00+00:00"},
        "orders": {"placed": 14, "by_status": {"completed": 2, "confirmed": 11, "cancelled": 1},
                   "net_revenue": "123456.50", "cancelled_in_period": 2},
        "top_products": [{"sku": "W-1", "name": "Widget", "quantity": 12, "revenue": "12000.00"}],
        "stock": {"products": 120, "out_of_stock": 4, "low_stock": 9,
                  "low_stock_items": [{"id": 1, "sku": "W-1", "name": "Widget", "stock": 3, "low_stock_threshold": 5}]},
        "audit": {"consistent": True, "discrepancies": []},
        "delivery": {"pending": 0, "failed": 0, "skipped": 1, "oldest_pending_seconds": None},
    }
    report.update(overrides)
    return report


def test_report_text_has_every_section():
    subject, body = render_report(make_report(), "Lahore Traders", "PKR", KARACHI)
    assert subject == "Daily report 12 Oct 2026 - Lahore Traders"
    assert "11 Oct 2026 08:00 to 12 Oct 2026 08:00 (Asia/Karachi)" in body      # shown in local time
    for expected in ("Placed: 14 (11 confirmed, 2 completed, 1 cancelled)", "PKR 123,456.50",
                     "Cancelled during this period: 2", "1. Widget (W-1): 12 unit(s), PKR 12,000.00",
                     "Out of stock: 4", "Widget (W-1): 3 left (alert level 5)", "Inventory ledger: consistent",
                     "0 waiting, 0 failed, 1 skipped"):
        assert expected in body, expected
    assert "ACTION NEEDED" not in body and "ACTION NEEDED" not in subject


def test_problems_are_flagged_at_the_top():
    report = make_report(audit={"consistent": False, "discrepancies": [{"product_id": 1}]},
                         delivery={"pending": 2, "failed": 3, "skipped": 0, "oldest_pending_seconds": 600})
    subject, body = render_report(report, "Lahore Traders", "PKR", KARACHI)
    assert subject.startswith("[ACTION NEEDED] ")
    assert body.index("ACTION NEEDED") < body.index("ORDERS")
    assert "1 product(s) whose stock does not match" in body and "3 email(s) failed permanently" in body
    assert "MISMATCH" in body and "oldest waiting: 10 min" in body


def test_quiet_day_report():
    report = make_report(orders={"placed": 0, "by_status": {}, "net_revenue": "0.00", "cancelled_in_period": 0},
                         top_products=[], stock={"products": 3, "out_of_stock": 0, "low_stock": 0,
                                                 "low_stock_items": []})
    _, body = render_report(report, "Lahore Traders", "PKR", KARACHI)
    assert "Placed: 0 (none)" in body and "No sales in this period." in body and "Needs reordering" not in body


def test_build_message_for_reports():
    msg = build_message("daily_report", recipient="boss@example.com", sender="shop@example.com",
                        company_name="Lahore Traders", currency="PKR", report=make_report(), tz=KARACHI)
    assert msg["To"] == "boss@example.com" and msg["Subject"].startswith("Daily report")
    assert "TOP PRODUCTS" in msg.get_content() and not list(msg.iter_attachments())
