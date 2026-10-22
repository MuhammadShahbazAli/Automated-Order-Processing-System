"""Builds and sends emails. Standard library only.

Backends (EMAIL_BACKEND):
  file  - writes .eml files into EMAIL_OUTBOX_DIR (default). No mail server needed; double-click to open.
  smtp  - sends for real using SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_STARTTLS.
"""
import os
import smtplib
from datetime import datetime
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


def build_message(kind: str, *, recipient: str, sender: str, company_name: str, currency: str,
                  order: dict | None = None, payload: dict | None = None,
                  invoice_pdf: bytes | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient

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
