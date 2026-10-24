"""Builds and sends emails. Standard library only.

Backends (EMAIL_BACKEND):
  file  - writes .eml files into EMAIL_OUTBOX_DIR (default). No mail server needed; double-click to open.
  smtp  - sends for real using SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_STARTTLS.
"""
import os
import smtplib
from datetime import datetime, timezone
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path


def _money(amount: str, currency: str) -> str:
    return f"{currency} {Decimal(amount):,.2f}".strip()


def _items_block(order: dict, currency: str) -> str:
    lines = []
    for item in order["items"]:
        lines.append(f"  {item['quantity']} x {item['name']} ({item['sku']})  -  "
                     f"{_money(item['line_total'], currency)}")
    return "\n".join(lines)


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _local(iso: str, tz) -> datetime:
    return datetime.fromisoformat(iso).astimezone(tz or timezone.utc)


def _fmt_dt(value: datetime) -> str:
    return f"{value.day:02d} {_MONTHS[value.month - 1]} {value.year} {value:%H:%M}"


def render_report(report: dict, company_name: str, currency: str, tz=None) -> tuple[str, str]:
    """Returns (subject, plain-text body) for a daily report."""
    start, end = _local(report["period"]["start"], tz), _local(report["period"]["end"], tz)
    label = getattr(tz, "key", "UTC")
    orders, stock, audit, delivery = report["orders"], report["stock"], report["audit"], report["delivery"]

    attention = []
    if not audit["consistent"]:
        attention.append(f"The inventory audit found {len(audit['discrepancies'])} product(s) whose stock does not "
                         "match the stock ledger. Check GET /inventory/audit.")
    if delivery["failed"]:
        attention.append(f"{delivery['failed']} email(s) failed permanently. See GET /notifications?status=failed "
                         "(POST /notifications/{id}/retry re-queues one; POST /notifications/{id}/dismiss clears one that can never work).")

    lines = [f"Daily report for {company_name}",
             f"Period: {_fmt_dt(start)} to {_fmt_dt(end)} ({label})", ""]
    if attention:
        lines += ["ACTION NEEDED"] + [f"  ! {a}" for a in attention] + [""]

    lifecycle = ["confirmed", "processing", "shipped", "completed", "cancelled"]
    by_status = orders["by_status"]
    breakdown = ", ".join(f"{by_status[st]} {st}" for st in lifecycle if by_status.get(st)) or "none"
    lines += ["ORDERS",
              f"  Placed: {orders['placed']} ({breakdown})",
              f"  Net revenue (excluding cancelled): {_money(orders['net_revenue'], currency)}",
              f"  Cancelled during this period: {orders['cancelled_in_period']}", ""]

    lines.append("TOP PRODUCTS")
    if report["top_products"]:
        for i, p in enumerate(report["top_products"], start=1):
            lines.append(f"  {i}. {p['name']} ({p['sku']}): {p['quantity']} unit(s), {_money(p['revenue'], currency)}")
    else:
        lines.append("  No sales in this period.")
    lines.append("")

    lines += ["STOCK",
              f"  Products: {stock['products']}  |  Out of stock: {stock['out_of_stock']}  |  Low stock: {stock['low_stock']}"]
    if stock["low_stock_items"]:
        lines.append("  Needs reordering:")
        for item in stock["low_stock_items"]:
            lines.append(f"    - {item['name']} ({item['sku']}): {item['stock']} left (alert level {item['low_stock_threshold']})")
    lines.append("")

    oldest = delivery.get("oldest_pending_seconds")
    lines += ["SYSTEM HEALTH",
              f"  Inventory ledger: {'consistent' if audit['consistent'] else 'MISMATCH'}",
              f"  Emails: {delivery['pending']} waiting, {delivery['failed']} failed, {delivery['skipped']} skipped"
              + (f" (oldest waiting: {oldest // 60} min)" if oldest else ""), ""]

    subject = f"Daily report {_fmt_dt(end)[:11]} - {company_name}"
    if attention:
        subject = "[ACTION NEEDED] " + subject
    return subject, "\n".join(lines)


def build_message(kind: str, *, recipient: str, sender: str, company_name: str, currency: str,
                  order: dict | None = None, payload: dict | None = None,
                  invoice_pdf: bytes | None = None, report: dict | None = None, tz=None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient

    if kind == "daily_report":
        assert report is not None
        subject, body = render_report(report, company_name, currency, tz)
        msg["Subject"] = subject
        msg.set_content(body)
        return msg

    if kind == "low_stock":
        p = payload or {}
        msg["Subject"] = f"Low stock: {p.get('name')} ({p.get('sku')})"
        msg.set_content(
            f"Stock for {p.get('name')} (SKU {p.get('sku')}) is down to {p.get('stock')} "
            f"(alert level: {p.get('threshold')}).\n\nPlease reorder.\n")
        return msg

    assert order is not None
    customer = order.get("customer") or {}
    name = customer.get("name") or "there"
    number = f"#{order['id']}"
    summary = (f"Order {number}  |  Invoice {order['invoice_number']}\n"
               f"{_items_block(order, currency)}\n"
               f"Total: {_money(order['total'], currency)}")
    sign_off = f"\nRegards,\n{company_name}\n"

    if kind == "order_confirmed":
        msg["Subject"] = f"Order {number} confirmed - {company_name}"
        body = (f"Hi {name},\n\nThank you for your order! We have received it and reserved your items.\n\n"
                f"{summary}\n\nYour invoice is attached as a PDF.\n")
    elif kind == "order_shipped":
        msg["Subject"] = f"Your order {number} has shipped - {company_name}"
        body = f"Hi {name},\n\nGood news: your order is on its way.\n\n{summary}\n"
    elif kind == "order_completed":
        msg["Subject"] = f"Your order {number} is complete - {company_name}"
        body = (f"Hi {name},\n\nYour order is complete. Thank you for shopping with us!\n\n{summary}\n\n"
                f"A copy of your invoice is attached.\n")
    elif kind == "order_cancelled":
        reason = order.get("cancel_reason")
        msg["Subject"] = f"Your order {number} was cancelled - {company_name}"
        body = (f"Hi {name},\n\nYour order has been cancelled."
                + (f" Reason: {reason}." if reason else "")
                + f"\n\n{summary}\n\nIf you have any questions, just reply to this email.\n")
    else:
        raise ValueError(f"Unknown notification type: {kind}")

    msg.set_content(body + sign_off)
    if invoice_pdf:
        msg.add_attachment(invoice_pdf, maintype="application", subtype="pdf",
                           filename=f"{order['invoice_number']}.pdf")
    return msg


class FileBackend:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def send(self, msg: EmailMessage, name_hint: str) -> str:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{name_hint}.eml"
        path.write_bytes(bytes(msg))
        return str(path)


class SmtpBackend:
    def __init__(self, host: str, port: int, user: str, password: str, starttls: bool):
        self.host, self.port, self.user, self.password, self.starttls = host, port, user, password, starttls

    def send(self, msg: EmailMessage, name_hint: str) -> str:
        with smtplib.SMTP(self.host, self.port, timeout=20) as smtp:
            if self.starttls:
                smtp.starttls()
            if self.user:
                smtp.login(self.user, self.password)
            smtp.send_message(msg)
        return f"smtp:{self.host}"


def backend_from_env():
    kind = os.environ.get("EMAIL_BACKEND", "file").lower()
    if kind == "smtp":
        host = os.environ.get("SMTP_HOST", "")
        if not host:
            raise RuntimeError("EMAIL_BACKEND=smtp requires SMTP_HOST")
        return SmtpBackend(host, int(os.environ.get("SMTP_PORT", "587")), os.environ.get("SMTP_USER", ""),
                           os.environ.get("SMTP_PASSWORD", ""), os.environ.get("SMTP_STARTTLS", "1") == "1")
    if kind == "file":
        return FileBackend(os.environ.get("EMAIL_OUTBOX_DIR", "outbox"))
    raise RuntimeError(f"Unknown EMAIL_BACKEND '{kind}' (use 'file' or 'smtp')")


def sender_from_env() -> str:
    return os.environ.get("EMAIL_FROM", "no-reply@localhost")
