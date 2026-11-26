import datetime as dt
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def to_pence(text, allow_negative=False):
    """Parse '12.50', '£1,200' etc. into whole pence."""
    s = (text or "").strip().replace("£", "").replace(",", "")
    if s == "":
        return 0
    try:
        value = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"'{text}' is not a valid amount")
    pence = int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if pence < 0 and not allow_negative:
        raise ValueError("Amounts can't be negative here")
    return pence


def money(pence):
    if pence is None:
        return ""
    sign = "-" if pence < 0 else ""
    p = abs(int(pence))
    return f"{sign}£{p // 100:,}.{p % 100:02d}"


def pence_input(pence):
    if pence is None:
        return ""
    sign = "-" if pence < 0 else ""
    p = abs(int(pence))
    return f"{sign}{p // 100}.{p % 100:02d}"


def pct(numerator, denominator):
    """Percentage to 1 d.p., or None when it can't be worked out."""
    if not denominator:
        return None
    return round(numerator * 100.0 / denominator, 1)


def parse_date(text, default=None):
    text = (text or "").strip()
    if not text:
        return default
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"'{text}' is not a valid date")


# UK personal tax year runs 6 April to 5 April.
def tax_year_of(d):
    return d.year if (d.month, d.day) >= (4, 6) else d.year - 1


def tax_year_range(start_year):
    return dt.date(start_year, 4, 6), dt.date(start_year + 1, 4, 5)


def tax_year_label(start_year):
    return f"{start_year}/{str(start_year + 1)[2:]}"


def period_key(d, period):
    """Return (sortable key, label) for the reporting period a date falls in."""
    if period == "week":
        start = d - dt.timedelta(days=d.weekday())
        return start.isoformat(), "w/c " + start.strftime("%d %b %Y")
    if period == "month":
        return d.strftime("%Y-%m"), d.strftime("%B %Y")
    if period == "year":
        return str(d.year), f"Calendar year {d.year}"
    y = tax_year_of(d)
    return str(y), "Tax year " + tax_year_label(y)
