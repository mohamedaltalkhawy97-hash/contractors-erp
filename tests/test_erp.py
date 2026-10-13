import datetime as dt
import re
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.models import Account, Company, JournalEntry, Project, User
from app.seed import create_company
from app.security import hash_password
from app.services import D, ZERO, trial_balance, validate_entry
from tests.conftest import csrf_of


def acc_id(db, cid, code):
    return db.scalar(select(Account.id).where(Account.company_id == cid, Account.code == code))


def post_journal(client, lines, date=None, action="post", desc="اختبار", path="/journal/new", token_page="/journal/new"):
    tok = csrf_of(client, token_page)
    data = {"csrf": tok, "date": date or str(dt.date.today().replace(month=1, day=15)), "description": desc, "reference": "", "action": action}
    data["account_id"] = [str(l[0]) for l in lines]; data["debit"] = [l[1] for l in lines]; data["credit"] = [l[2] for l in lines]
    data["line_desc"] = [""] * len(lines); data["project_id"] = [str(l[3]) if len(l) > 3 and l[3] else "" for l in lines]; data["cc_id"] = [""] * len(lines)
    return client.post(path, data=data, follow_redirects=False)


# ---------------- accounting rules
def test_chart_loaded_and_levels():
    with SessionLocal() as db:
        c = db.scalar(select(Company))
        a = db.scalar(select(Account).where(Account.company_id == c.id, Account.code == 10105))
        assert a.cls == 10 and a.level == 3 and a.parent_code == 10100 and a.nature == "مدين"
        a = db.scalar(select(Account).where(Account.company_id == c.id, Account.code == 2101))
        assert a.nature == "دائن" and a.type_name == "خصوم"
        assert db.scalar(select(Account).where(Account.company_id == c.id, Account.code == 5101)).project_required


def test_money_parsing():
    assert D("1,234.567") == Decimal("1234.57") and D("١٢٣٫٥") == Decimal("123.50") and D("") == ZERO
    with pytest.raises(ValueError):
        D("abc")


def test_demo_trial_balance_is_balanced():
    with SessionLocal() as db:
        c = db.scalar(select(Company))
        rows, tot = trial_balance(db, c.id, c.fy_start, c.fy_end)
        assert tot["balanced"] and tot["debit"] == tot["credit"] and tot["debit"] > 0
        by = {r["code"]: r for r in rows}
        assert by[1000]["closing"] == by[1101]["closing"] + by[1102]["closing"] + by[1201]["closing"] + by[1203]["closing"] + by[1404]["closing"]
        # independent expectation from the demo script
        assert by[4101]["credit"] == Decimal("2040000.00") and by[1102]["closing"] == Decimal("3020000.00") and by[1101]["closing"] == Decimal("100000.00")


def test_login_required_and_wrong_password(client):
    c2 = TestClient(app)
    assert c2.get("/", follow_redirects=False).status_code == 303
    r = c2.post("/login", data={"username": "admin", "password": "bad"})
    assert "غير صحيحة" in r.text


def test_csrf_enforced(admin):
    r = admin.post("/m/customers/new", data={"code": "X", "name": "Y"})
    assert r.status_code == 403


def test_unbalanced_entry_is_never_posted(admin):
    with SessionLocal() as db:
        c = db.scalar(select(Company)); a1, a2 = acc_id(db, c.id, 7199), acc_id(db, c.id, 2403)
    before = None
    r = post_journal(admin, [(a1, "1000", ""), (a2, "", "900")])
    assert r.status_code == 303  # saved as draft, redirected to edit with error flash
    with SessionLocal() as db:
        e = db.scalar(select(JournalEntry).order_by(JournalEntry.id.desc()))
        assert e.status == "draft"


def test_post_balanced_entry_and_immutability_and_reversal(admin):
    with SessionLocal() as db:
        c = db.scalar(select(Company)); cash, cap = acc_id(db, c.id, 1101), acc_id(db, c.id, 3101)
    r = post_journal(admin, [(cash, "5,000.50", ""), (cap, "", "5000.50")])
    assert r.status_code == 303
    eid = int(r.headers["location"].split("/")[-1])
    with SessionLocal() as db:
        e = db.get(JournalEntry, eid)
        assert e.status == "posted" and e.total_debit == Decimal("5000.50")
    # cannot edit posted entry
    assert admin.get(f"/journal/{eid}/edit").status_code == 400
    # cannot delete posted entry
    tok = csrf_of(admin, f"/journal/{eid}")
    admin.post(f"/journal/{eid}/delete", data={"csrf": tok})
    with SessionLocal() as db:
        assert db.get(JournalEntry, eid) is not None
    # reversal creates opposite posted entry; second reversal refused
    tok = csrf_of(admin, f"/journal/{eid}")
    r = admin.post(f"/journal/{eid}/reverse", data={"csrf": tok, "date": ""}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        rev = db.scalar(select(JournalEntry).where(JournalEntry.reversal_of_id == eid))
        assert rev.status == "posted" and rev.lines[0].credit == Decimal("5000.50")
    tok = csrf_of(admin, f"/journal/{eid}")
    admin.post(f"/journal/{eid}/reverse", data={"csrf": tok, "date": ""})
    with SessionLocal() as db:
        assert len(db.scalars(select(JournalEntry).where(JournalEntry.reversal_of_id == eid)).all()) == 1


def test_validation_rules(admin):
    with SessionLocal() as db:
        c = db.scalar(select(Company))
        group, cost, cash = acc_id(db, c.id, 5100), acc_id(db, c.id, 5101), acc_id(db, c.id, 1101)
        e = JournalEntry(company_id=c.id, number="T-1", date=c.fy_start - dt.timedelta(days=1), status="draft")
        from app.models import JournalLine
        e.lines.append(JournalLine(company_id=c.id, account_id=group, debit=Decimal("10"), credit=Decimal("0")))
        e.lines.append(JournalLine(company_id=c.id, account_id=cost, debit=Decimal("5"), credit=Decimal("5")))
        errs = " | ".join(validate_entry(db, e, c))
        assert "خارج السنة المالية" in errs and "حساب رئيسي" in errs and "مدين ودائن معاً" in errs and "يتطلب كود مشروع" in errs and "غير متوازن" in errs


def test_project_required_and_default_cost_center(admin):
    with SessionLocal() as db:
        c = db.scalar(select(Company)); p = db.scalar(select(Project).where(Project.company_id == c.id)); cost, ap = acc_id(db, c.id, 5101), acc_id(db, c.id, 2101)
        pid, cc = p.id, p.cost_center_id
    r = post_journal(admin, [(cost, "100", "", pid), (ap, "", "100")])
    eid = int(r.headers["location"].split("/")[-1])
    with SessionLocal() as db:
        e = db.get(JournalEntry, eid)
        assert e.status == "posted" and e.lines[0].cost_center_id == cc


def test_ledger_and_tb_pages_render_and_match(admin):
    r = admin.get("/reports/trial-balance"); assert r.status_code == 200 and "الميزان متوازن" in r.text
    with SessionLocal() as db:
        c = db.scalar(select(Company)); a = acc_id(db, c.id, 1102)
    r = admin.get(f"/reports/ledger?account={a}"); assert r.status_code == 200 and "رصيد آخر الفترة" in r.text
    assert admin.get("/").status_code == 200 and admin.get("/m/accounts").status_code == 200 and admin.get("/m/projects").status_code == 200


def test_master_crud_duplicate_code_and_validation(admin):
    tok = csrf_of(admin, "/m/customers/new")
    r = admin.post("/m/customers/new", data={"csrf": tok, "code": "C-NEW", "name": "عميل جديد", "payment_terms": "45", "opening_balance": "1,000", "credit_limit": "5000", "active": "on"}, follow_redirects=False)
    assert r.status_code == 303
    r = admin.post("/m/customers/new", data={"csrf": tok, "code": "C-NEW", "name": "مكرر"})
    assert "مستخدم من قبل" in r.text
    r = admin.post("/m/customers/new", data={"csrf": tok, "code": "", "name": ""})
    assert "مطلوب" in r.text
    r = admin.post("/m/accounts/new", data={"csrf": tok, "code": "5999", "name": "حساب بلا أب", "active": "on"})
    assert r.status_code == 200
    r = admin.post("/m/accounts/new", data={"csrf": tok, "code": "5106", "name": "مواد أخرى", "active": "on", "project_required": "on", "cc_required": "on"}, follow_redirects=False)
    assert r.status_code == 303


# ---------------- roles and tenant isolation
def test_viewer_cannot_write_and_other_company_is_isolated(admin):
    with SessionLocal() as db:
        c1 = db.scalar(select(Company))
        c2 = create_company(db, "شركة أخرى")
        db.add(User(company_id=c2.id, username="other", password_hash=hash_password("Other#12345"), role="admin"))
        db.add(User(company_id=c1.id, username="viewer", password_hash=hash_password("Viewer#12345"), role="viewer"))
        db.commit(); e1 = db.scalar(select(JournalEntry).where(JournalEntry.company_id == c1.id)).id
    v = TestClient(app); v.post("/login", data={"username": "viewer", "password": "Viewer#12345"})
    assert v.get("/journal").status_code == 200
    assert v.get("/journal/new", follow_redirects=False).status_code == 403
    assert v.get("/admin/users", follow_redirects=False).status_code == 403
    o = TestClient(app); o.post("/login", data={"username": "other", "password": "Other#12345"})
    assert "JV-0001" not in o.get("/journal").text
    assert o.get(f"/journal/{e1}").status_code == 404
    rows_html = o.get("/reports/trial-balance").text
    assert "2,000,000" not in rows_html


def test_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_all_pages_render(admin):
    with SessionLocal() as db:
        c = db.scalar(select(Company)); p = db.scalar(select(Project).where(Project.company_id == c.id))
        e = db.scalar(select(JournalEntry).where(JournalEntry.company_id == c.id)).id
        cust = db.scalar(select(__import__("app.models", fromlist=["Customer"]).Customer).where(__import__("app.models", fromlist=["Customer"]).Customer.company_id == c.id)).id
    for url in ["/", "/m/accounts", "/m/customers", "/m/suppliers", "/m/projects", "/m/cost-centers", "/m/customers/new", "/m/projects/new", "/m/accounts/new",
                f"/m/projects/{p.id}", f"/m/customers/{cust}", "/journal", "/journal?status=draft&q=x", "/journal/new", f"/journal/{e}", "/reports/trial-balance?hide_groups=1",
                "/reports/ledger", "/admin/users", "/admin/settings", "/admin/audit", "/m/accounts?q=مواد"]:
        r = admin.get(url)
        assert r.status_code == 200, (url, r.status_code)
    assert "المشاريع" in admin.get("/m/projects").text
