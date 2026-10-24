"""Dependency-free PDF invoice generator (A4, built-in Helvetica fonts).

Built-in PDF fonts only cover Latin-1/Windows-1252 characters; anything else (e.g. Urdu script)
is printed as '?'. Embedding a TTF font would lift that limit if you ever need it.
"""
from datetime import datetime
from decimal import Decimal

PAGE_W, PAGE_H = 595.28, 841.89
MARGIN = 48.0
RIGHT = PAGE_W - MARGIN

# Helvetica glyph widths (1/1000 em) for ASCII 32..126.
_W_REGULAR = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
]
_W_BOLD = [
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
    975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
    333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
    611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584,
]
assert len(_W_REGULAR) == len(_W_BOLD) == 95

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_GRAY = (0.40, 0.40, 0.40)
_BLACK = (0.0, 0.0, 0.0)


def _encode(text: str) -> bytes:
    return str(text).encode("cp1252", errors="replace")


def text_width(text: str, size: float, bold: bool = False) -> float:
    table = _W_BOLD if bold else _W_REGULAR
    return sum((table[b - 32] if 32 <= b <= 126 else 556) for b in _encode(text)) * size / 1000.0


def _pdf_string(raw: bytes) -> bytes:
    out = bytearray(b"(")
    for b in raw:
        if b in (0x5C, 0x28, 0x29):          # \ ( )
            out += b"\\" + bytes([b])
        elif 32 <= b <= 126:
            out.append(b)
        else:                                  # keep the file 7-bit clean
            out += b"\\%03o" % b
    out += b")"
    return bytes(out)


def _fit(text: str, max_width: float, size: float, bold: bool = False) -> str:
    if text_width(text, size, bold) <= max_width:
        return text
    while text and text_width(text + "...", size, bold) > max_width:
        text = text[:-1]
    return text + "..."


def _wrap(text: str, max_width: float, size: float, max_lines: int, bold: bool = False) -> list[str]:
    words, lines, current = str(text).split(), [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if text_width(trial, size, bold) <= max_width:
            current = trial
            continue
        if current:
            lines.append(current)
        current = word
        while text_width(current, size, bold) > max_width and len(current) > 1:   # one very long word
            cut = len(current)
            while cut > 1 and text_width(current[:cut], size, bold) > max_width:
                cut -= 1
            lines.append(current[:cut])
            current = current[cut:]
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = _fit(lines[-1] + " ...", max_width, size, bold)
    return lines or [""]


def _money(amount: str, currency: str) -> str:
    return f"{currency} {Decimal(amount):,.2f}"


def _date(value) -> str:
    if isinstance(value, datetime):
        return f"{value.day:02d} {_MONTHS[value.month - 1]} {value.year}"
    return str(value or "")


class _Page:
    def __init__(self):
        self.ops: list[bytes] = []

    def text(self, x, y, s, size=10, bold=False, align="left", color=_BLACK):
        s = str(s)
        width = text_width(s, size, bold)
        if align == "right":
            x -= width
        elif align == "center":
            x -= width / 2
        r, g, b = color
        self.ops.append(
            b"BT /%s %.2f Tf %.3f %.3f %.3f rg %.2f %.2f Td " % (b"F2" if bold else b"F1", size, r, g, b, x, y)
            + _pdf_string(_encode(s)) + b" Tj ET")

    def rect(self, x, y, w, h, gray=0.93):
        self.ops.append(b"%.3f g %.2f %.2f %.2f %.2f re f" % (gray, x, y, w, h))

    def line(self, x1, y1, x2, y2, width=0.6, gray=0.6):
        self.ops.append(b"%.3f G %.2f w %.2f %.2f m %.2f %.2f l S" % (gray, width, x1, y1, x2, y2))

    def stamp(self, word):
        # Inserted FIRST so it sits behind the invoice text and never hides a number.
        self.ops.insert(0, b"BT /F2 80 Tf 0.95 0.80 0.80 rg 0.8660 0.5 -0.5 0.8660 130 280 Tm "
                        + _pdf_string(_encode(word)) + b" Tj ET")

    def content(self) -> bytes:
        return b"\n".join(self.ops)


def build_invoice_pdf(order: dict, company: dict) -> bytes:
    """order: the dict returned by services.get_order.  company: {name, address_lines, currency}."""
    currency = company.get("currency", "")
    status = order["status"]
    customer = order.get("customer") or {}

    # column right/left edges
    x_no, x_sku, x_desc = MARGIN, MARGIN + 24, MARGIN + 114
    w_sku, w_desc = 84, 188
    x_qty, x_unit, x_amt = 392.0, 470.0, RIGHT

    pages: list[_Page] = []

    def new_page(first: bool) -> tuple[_Page, float]:
        page = _Page()
        pages.append(page)
        y = PAGE_H - MARGIN
        if first:
            page.text(MARGIN, y - 14, company.get("name", ""), 17, bold=True)
            ay = y - 30
            for line in company.get("address_lines", [])[:4]:
                page.text(MARGIN, ay, _fit(line, 260, 9), 9, color=_GRAY)
                ay -= 12
            page.text(RIGHT, y - 18, "INVOICE", 24, bold=True, align="right")
            page.text(RIGHT, y - 36, order["invoice_number"], 11, bold=True, align="right")
            page.text(RIGHT, y - 50, f"Date: {_date(order['created_at'])}", 10, align="right", color=_GRAY)
            page.text(RIGHT, y - 63, f"Order #{order['id']}  |  Status: {status.capitalize()}", 10,
                      align="right", color=_GRAY)
            top = y - 100
            page.line(MARGIN, top, RIGHT, top)
            by = top - 20
            page.text(MARGIN, by, "BILL TO", 8, bold=True, color=_GRAY)
            by -= 15
            page.text(MARGIN, by, _fit(customer.get("name") or "-", 280, 11, True), 11, bold=True)
            for value in (customer.get("email"), customer.get("phone")):
                if value:
                    by -= 13
                    page.text(MARGIN, by, _fit(value, 280, 10), 10)
            if customer.get("address"):
                for line in _wrap(customer["address"], 280, 10, 3):
                    by -= 13
                    page.text(MARGIN, by, line, 10)
            y = min(by, top - 20) - 28
        else:
            page.text(MARGIN, y - 12, company.get("name", ""), 12, bold=True)
            page.text(RIGHT, y - 12, f"{order['invoice_number']} (continued)", 10, align="right", color=_GRAY)
            y -= 40
        # table header
        page.rect(MARGIN, y - 16, RIGHT - MARGIN, 20)
        page.text(x_no + 4, y - 10, "#", 9, bold=True)
        page.text(x_sku, y - 10, "SKU", 9, bold=True)
        page.text(x_desc, y - 10, "Description", 9, bold=True)
        page.text(x_qty, y - 10, "Qty", 9, bold=True, align="right")
        page.text(x_unit, y - 10, "Unit price", 9, bold=True, align="right")
        page.text(x_amt - 4, y - 10, "Amount", 9, bold=True, align="right")
        return page, y - 16

    page, y = new_page(first=True)
    bottom_limit = MARGIN + 50          # keep room for the footer

    for number, item in enumerate(order["items"], start=1):
        desc_lines = _wrap(item["name"], w_desc, 10, 3)
        row_h = 13 * len(desc_lines) + 8
        if y - row_h < bottom_limit:
            page, y = new_page(first=False)
        text_y = y - 14
        page.text(x_no + 4, text_y, str(number), 10)
        page.text(x_sku, text_y, _fit(item["sku"], w_sku, 9), 9)
        for i, line in enumerate(desc_lines):
            page.text(x_desc, text_y - 13 * i, line, 10)
        page.text(x_qty, text_y, str(item["quantity"]), 10, align="right")
        page.text(x_unit, text_y, f"{Decimal(item['unit_price']):,.2f}", 10, align="right")
        page.text(x_amt - 4, text_y, f"{Decimal(item['line_total']):,.2f}", 10, align="right")
        y -= row_h
        page.line(MARGIN, y, RIGHT, y, width=0.4, gray=0.85)

    if y - 70 < bottom_limit:
        page, y = new_page(first=False)
    y -= 26
    total_text = _money(order["total"], currency)
    amount_left = x_amt - 4 - text_width(total_text, 12, bold=True)
    page.line(min(x_unit - 90, amount_left - 80), y + 14, RIGHT, y + 14, width=0.8, gray=0.3)
    page.text(amount_left - 14, y - 2, "TOTAL", 12, bold=True, align="right")
    page.text(x_amt - 4, y - 2, total_text, 12, bold=True, align="right")
    y -= 40
    page.text(MARGIN, y, "Thank you for your order.", 10, color=_GRAY)
    if status == "cancelled":
        reason = order.get("cancel_reason")
        page.text(MARGIN, y - 14, "This order was cancelled." + (f" Reason: {reason}" if reason else ""),
                  9, color=_GRAY)

    total_pages = len(pages)
    for index, p in enumerate(pages, start=1):
        if status == "cancelled":
            p.stamp("CANCELLED")
        p.line(MARGIN, MARGIN + 18, RIGHT, MARGIN + 18, width=0.4, gray=0.8)
        p.text(PAGE_W / 2, MARGIN + 4, f"{order['invoice_number']}  |  Page {index} of {total_pages}", 8,
               align="center", color=_GRAY)

    return _assemble([p.content() for p in pages], order)


def _assemble(contents: list[bytes], order: dict) -> bytes:
    n = len(contents)
    size = 6 + 2 * n                                   # objects 1 .. size-1
    buf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}

    def add(num: int, body: bytes) -> None:
        offsets[num] = len(buf)
        buf.extend(b"%d 0 obj\n" % num + body + b"\nendobj\n")

    created = order["created_at"]
    stamp = created.strftime("D:%Y%m%d%H%M%SZ") if isinstance(created, datetime) else "D:20000101000000Z"
    kids = b" ".join(b"%d 0 R" % (6 + 2 * i) for i in range(n))

    add(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    add(2, b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % n)
    add(3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    add(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    add(5, b"<< /Title " + _pdf_string(_encode(f"Invoice {order['invoice_number']}"))
        + b" /Producer (Inventory Management API) /CreationDate (" + stamp.encode() + b") >>")
    for i, content in enumerate(contents):
        page_id, content_id = 6 + 2 * i, 7 + 2 * i
        add(page_id, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] "
                     b"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents %d 0 R >>"
            % (PAGE_W, PAGE_H, content_id))
        add(content_id, b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")

    xref_pos = len(buf)
    buf.extend(b"xref\n0 %d\n0000000000 65535 f \n" % size)
    for num in range(1, size):
        buf.extend(b"%010d 00000 n \n" % offsets[num])
    buf.extend(b"trailer\n<< /Size %d /Root 1 0 R /Info 5 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref_pos))
    return bytes(buf)
