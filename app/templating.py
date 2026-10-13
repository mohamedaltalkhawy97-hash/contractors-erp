import datetime as dt
from decimal import Decimal
from pathlib import Path
from fastapi.templating import Jinja2Templates

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


def money(v):
    if v is None or v == "":
        return "-"
    v = Decimal(v)
    return "-" if v == 0 else f"{v:,.2f}"


def pct(v):
    return f"{Decimal(v) * 100:.1f}%"


templates.env.filters["money"] = money
templates.env.filters["pct"] = pct
templates.env.filters["dstr"] = lambda d: d.strftime("%Y-%m-%d") if d else ""
