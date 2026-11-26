import io
import os
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image as RLImage
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .util import money

INK = colors.HexColor("#1f2933")
MUTED = colors.HexColor("#6b7280")
ACCENT = colors.HexColor("#1d4ed8")
RULE = colors.HexColor("#d1d5db")
TINT = colors.HexColor("#f3f4f6")


def _p(text, style):
    return Paragraph(escape(text or "").replace("\n", "<br/>"), style)


def _date(iso):
    if not iso:
        return ""
    y, m, d = iso.split("-")
    return f"{d}/{m}/{y}"


def build_pdf(doc, customer, lines, settings, logo_path=None, paid=0, total=0):
    """doc/customer: sqlite rows.  lines: list of dicts with description, qty, unit_price, serials."""
    is_invoice = doc["kind"] == "invoice"
    base = getSampleStyleSheet()["Normal"]
    normal = ParagraphStyle("n", parent=base, fontName="Helvetica", fontSize=9.5, leading=13, textColor=INK)
    small = ParagraphStyle("s", parent=normal, fontSize=8, leading=10.5, textColor=MUTED)
    serial = ParagraphStyle("serial", parent=normal, fontSize=8, leading=10.5, textColor=MUTED, leftIndent=0)
    bold = ParagraphStyle("b", parent=normal, fontName="Helvetica-Bold")
    label = ParagraphStyle("l", parent=small, fontName="Helvetica-Bold", textColor=MUTED)
    title = ParagraphStyle("t", parent=normal, fontName="Helvetica-Bold", fontSize=24, leading=28,
                           textColor=ACCENT, alignment=2)
    right = ParagraphStyle("r", parent=normal, alignment=2)
    right_bold = ParagraphStyle("rb", parent=right, fontName="Helvetica-Bold")
    biz_name = ParagraphStyle("bn", parent=normal, fontName="Helvetica-Bold", fontSize=14, leading=17)

    buf = io.BytesIO()
    pdf = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"{'Invoice' if is_invoice else 'Quote'} {doc['number'] or ''}".strip(),
        author=settings.get("business_name", ""),
    )
    width = A4[0] - 40 * mm
    story = []

    # --- header: logo / business name on the left, document title on the right
    left = []
    if logo_path and os.path.exists(logo_path):
        try:
            from PIL import Image

            with Image.open(logo_path) as im:
                w, h = im.size
            max_w, max_h = 60 * mm, 24 * mm
            scale = min(max_w / w, max_h / h)
            left.append(RLImage(logo_path, width=w * scale, height=h * scale, hAlign="LEFT"))
        except Exception:
            left.append(_p(settings.get("business_name", ""), biz_name))
    else:
        left.append(_p(settings.get("business_name", ""), biz_name))
    head = Table(
        [[left, [_p("INVOICE" if is_invoice else "QUOTE", title), _p(doc["number"] or "DRAFT", right_bold)]]],
        colWidths=[width * 0.55, width * 0.45],
    )
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [head, Spacer(1, 8 * mm)]

    # --- from / to / dates
    from_lines = [settings.get("business_name", "")]
    for key in ("business_address", "business_email", "business_phone"):
        if settings.get(key):
            from_lines.append(settings[key])
    to_lines = []
    if customer:
        to_lines = [customer["name"]]
        for key in ("address", "email", "phone"):
            if customer[key]:
                to_lines.append(customer[key])

    meta = [[_p("Date", label), _p(_date(doc["issue_date"]), normal)]]
    if doc["due_date"]:
        meta.append([_p("Payment due" if is_invoice else "Valid until", label), _p(_date(doc["due_date"]), normal)])
    meta_tbl = Table(meta, colWidths=[24 * mm, 30 * mm])
    meta_tbl.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 1),
                                  ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    info = Table(
        [[_p("FROM", label), _p("BILL TO" if is_invoice else "PREPARED FOR", label), ""],
         [_p("\n".join(from_lines), normal), _p("\n".join(to_lines), normal), meta_tbl]],
        colWidths=[width * 0.36, width * 0.36, width * 0.28],
    )
    info.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 6)]))
    story += [info, Spacer(1, 8 * mm)]

    # --- line items
    rows = [[_p("Description", label), _p("Qty", ParagraphStyle("qh", parent=label, alignment=2)),
             _p("Unit price", ParagraphStyle("uh", parent=label, alignment=2)),
             _p("Amount", ParagraphStyle("ah", parent=label, alignment=2))]]
    for ln in lines:
        cell = [_p(ln["description"], normal)]
        if ln.get("serials"):
            cell.append(_p("Serial no: " + ", ".join(ln["serials"]), serial))
        rows.append([cell, _p(str(ln["qty"]), right), _p(money(ln["unit_price"]), right),
                     _p(money(ln["qty"] * ln["unit_price"]), right)])
    tbl = Table(rows, colWidths=[width - 85 * mm, 15 * mm, 35 * mm, 35 * mm], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), TINT),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, RULE),
        ("LINEBELOW", (0, 1), (-1, -1), 0.3, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (0, -1), 4),
    ]))
    story += [tbl, Spacer(1, 4 * mm)]

    # --- totals
    trows = [[_p("Total", right_bold), _p(money(total), right_bold)]]
    if is_invoice and paid:
        trows.append([_p("Paid", right), _p(money(-paid), right)])
        trows.append([_p("Balance due", right_bold), _p(money(total - paid), right_bold)])
    ttbl = Table(trows, colWidths=[35 * mm, 35 * mm], hAlign="RIGHT")
    ttbl.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, 0), 0.8, INK), ("TOPPADDING", (0, 0), (-1, -1), 3),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story.append(ttbl)
    if is_invoice and total > 0 and paid >= total:
        story += [Spacer(1, 3 * mm), _p("PAID IN FULL - thank you", ParagraphStyle("paid", parent=bold, textColor=colors.HexColor("#15803d"), alignment=2))]

    # --- payment details, notes, footer
    story.append(Spacer(1, 8 * mm))
    if is_invoice and settings.get("bank_details"):
        story += [_p("PAYMENT DETAILS", label), _p(settings["bank_details"], normal), Spacer(1, 4 * mm)]
    if doc["notes"]:
        story += [_p("NOTES", label), _p(doc["notes"], normal), Spacer(1, 4 * mm)]
    footer = settings.get("invoice_footer" if is_invoice else "quote_footer", "")
    if footer:
        story += [_p(footer, small)]

    pdf.build(story)
    return buf.getvalue()
