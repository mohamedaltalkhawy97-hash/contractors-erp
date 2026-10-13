import datetime as dt
import secrets
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.orm import Session
from . import config
from .chart_data import CHART
from .db import Base, engine, SessionLocal
from .models import Account, Company, CostCenter, Customer, JournalEntry, JournalLine, Project, Supplier, User
from .security import hash_password
from .services import next_number, post_entry


def load_chart(db: Session, company_id: int):
    for code, name in CHART:
        leaf = code % 100 != 0
        db.add(Account(company_id=company_id, code=code, name=name, active=True,
                       project_required=bool(leaf and (5000 <= code < 6000 or code in (4101, 4190))),
                       cc_required=bool(leaf and 5000 <= code < 9000)))


def create_company(db: Session, name: str, fy_start: dt.date | None = None) -> Company:
    c = Company(name=name, fy_start=fy_start or dt.date(dt.date.today().year, 1, 1))
    db.add(c); db.flush()
    load_chart(db, c.id)
    return c


def init_db() -> str | None:
    """Create tables; on first run create the company, the admin user and (optionally) demo data. Returns generated password if any."""
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if db.scalar(select(User).limit(1)):
            return None
        company = create_company(db, config.COMPANY_NAME)
        pwd = config.ADMIN_PASSWORD or secrets.token_urlsafe(9)
        db.add(User(company_id=company.id, username=config.ADMIN_USERNAME, full_name="مدير النظام", password_hash=hash_password(pwd), role="admin"))
        db.flush()
        if config.LOAD_DEMO:
            load_demo(db, company)
        db.commit()
        return None if config.ADMIN_PASSWORD else pwd


def load_demo(db: Session, company: Company):
    cid = company.id
    for code, name, kind in [("CC-HO", "المكتب الرئيسي", "مكتب رئيسي"), ("CC-P001", "موقع مجمع النخيل", "مشروع"), ("CC-P002", "موقع مستشفى الشفاء", "مشروع")]:
        db.add(CostCenter(company_id=cid, code=code, name=name, kind=kind))
    c1 = Customer(company_id=cid, code="C-001", name="شركة الإسكندرية للتطوير العقاري", payment_terms=30, credit_limit=Decimal("5000000"))
    c2 = Customer(company_id=cid, code="C-003", name="مستشفى الشفاء التخصصي", payment_terms=30, credit_limit=Decimal("2500000"))
    s1 = Supplier(company_id=cid, code="S-001", name="شركة الدلتا لمواد البناء", payment_terms=30)
    s2 = Supplier(company_id=cid, code="S-101", name="مؤسسة البناء الحديث للأعمال الكهربائية", kind="مقاول باطن", payment_terms=30)
    db.add_all([c1, c2, s1, s2]); db.flush()
    cc = {c.code: c for c in db.scalars(select(CostCenter).where(CostCenter.company_id == cid))}
    p1 = Project(company_id=cid, code="P-001", name="مجمع النخيل السكني - الإسكندرية", customer_id=c1.id, contract_no="CN-2026-001", contract_value=Decimal("12000000"),
                 status="جاري", cost_center_id=cc["CC-P001"].id, retention_pct=Decimal("0.05"), advance_pct=Decimal("0.10"), manager="م. أحمد سمير")
    p2 = Project(company_id=cid, code="P-002", name="توسعة مستشفى الشفاء", customer_id=c2.id, contract_no="CN-2026-002", contract_value=Decimal("6500000"),
                 status="جاري", cost_center_id=cc["CC-P002"].id, retention_pct=Decimal("0.05"), advance_pct=Decimal("0.10"), manager="م. منى فتحي")
    db.add_all([p1, p2]); db.flush()
    acc = {a.code: a for a in db.scalars(select(Account).where(Account.company_id == cid))}
    admin = db.scalar(select(User).where(User.company_id == cid))
    y = company.fy_start.year

    def entry(date, desc, lines):
        e = JournalEntry(company_id=cid, number=next_number(db, cid), date=date, description=desc, status="draft", created_by=admin.id)
        for code, dr, cr, proj in lines:
            e.lines.append(JournalLine(company_id=cid, account_id=acc[code].id, debit=Decimal(dr), credit=Decimal(cr), project_id=proj.id if proj else None))
        db.add(e); db.flush(); post_entry(db, e, company, admin)

    entry(dt.date(y, 1, 1), "رصيد افتتاحي - رأس المال", [(1102, "2000000", "0", None), (1101, "100000", "0", None), (3101, "0", "2100000", None)])
    entry(dt.date(y, 2, 10), "شراء مواد خرسانة للمشروع", [(5101, "300000", "0", p1), (1404, "42000", "0", None), (2101, "0", "342000", None)])
    entry(dt.date(y, 3, 5), "مستخلص رقم 1 - مجمع النخيل", [(1201, "1734000", "0", None), (1203, "102000", "0", None), (2201, "204000", "0", None), (4101, "0", "2040000", p1)])
    entry(dt.date(y, 3, 20), "دفعة مقدمة من العميل", [(1102, "1200000", "0", None), (2201, "0", "1200000", None)])
    entry(dt.date(y, 4, 10), "أجور عمالة مباشرة", [(5201, "180000", "0", p1), (1102, "0", "180000", None)])
