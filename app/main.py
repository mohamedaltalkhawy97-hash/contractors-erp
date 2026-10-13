import datetime as dt
import time
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import config
from .db import get_db
from .models import (ACCOUNT_TYPES, PROJECT_STATUS, ROLES, Account, AuditLog, Company, CostCenter, Customer, JournalEntry, JournalLine, Project, Supplier, User)
from .security import hash_password, new_csrf, verify_password
from .seed import init_db
from .services import D, audit, ledger, next_number, post_entry, reverse_entry, trial_balance, validate_entry
from .templating import templates

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(_app):
    global GENERATED_PASSWORD
    GENERATED_PASSWORD = init_db()
    if GENERATED_PASSWORD:
        print(f"\n=== أول تشغيل: اسم المستخدم {config.ADMIN_USERNAME}  كلمة المرور {GENERATED_PASSWORD}  (غيّرها فوراً) ===\n", flush=True)
    yield


app = FastAPI(title="نظام المقاولات ERP", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, https_only=config.SESSION_HTTPS_ONLY, same_site="lax", max_age=60 * 60 * 12)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
GENERATED_PASSWORD = None


# ------------------------------------------------------------------ auth helpers
class NeedLogin(Exception):
    pass


@app.exception_handler(NeedLogin)
def _need_login(request, exc):
    return RedirectResponse("/login", status_code=303)


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if not user or not user.is_active:
        raise NeedLogin()
    return user


def can_write(user: User) -> bool:
    return user.role in ("admin", "accountant")


def need_write(user: User = Depends(current_user)) -> User:
    if not can_write(user):
        raise HTTPException(403, "ليست لديك صلاحية التعديل")
    return user


def need_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "للمدير فقط")
    return user


def ctx(request: Request, user: User | None, **kw):
    if "csrf" not in request.session:
        request.session["csrf"] = new_csrf()
    flash = request.session.pop("flash", None)
    return dict(request=request, user=user, csrf=request.session["csrf"], flash=flash, can_write=bool(user and can_write(user)), roles=ROLES, **kw)


def render(request, name, user, **kw):
    return templates.TemplateResponse(request, name, ctx(request, user, **kw))


async def get_form(request: Request):
    form = await request.form()
    if form.get("csrf") != request.session.get("csrf"):
        raise HTTPException(403, "رمز الحماية غير صالح - أعد تحميل الصفحة")
    return form


def flash(request, msg, kind="ok"):
    request.session["flash"] = {"msg": msg, "kind": kind}


# ------------------------------------------------------------------ login
_FAILS: dict[str, list[float]] = {}


@app.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    db.execute(text("select 1"))
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html", None, error=None)


@app.post("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    username = (form.get("username") or "").strip()
    key = f"{request.client.host if request.client else '-'}|{username}"
    now = time.time()
    fails = [t for t in _FAILS.get(key, []) if now - t < 300]
    if len(fails) >= 5:
        return render(request, "login.html", None, error="محاولات كثيرة خاطئة. انتظر 5 دقائق ثم حاول مرة أخرى.")
    user = db.scalar(select(User).where(User.username == username))
    if not user or not user.is_active or not verify_password(form.get("password") or "", user.password_hash):
        fails.append(now); _FAILS[key] = fails
        return render(request, "login.html", None, error="اسم المستخدم أو كلمة المرور غير صحيحة")
    _FAILS.pop(key, None)
    request.session.clear()
    request.session["uid"] = user.id
    request.session["csrf"] = new_csrf()
    audit(db, user, "login", "user", user.id); db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ------------------------------------------------------------------ dashboard
def default_period(company: Company):
    today = dt.date.today()
    end = min(max(today, company.fy_start), company.fy_end)
    return company.fy_start, end


def pdate(s, default):
    try:
        return dt.date.fromisoformat(s) if s else default
    except ValueError:
        return default


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    company = user.company
    start, end = default_period(company)
    rows, tot = trial_balance(db, company.id, start, end)
    by = {r["account"].code: r for r in rows}
    g = lambda code: by.get(code, {}).get("closing", Decimal("0"))
    mv = lambda code: by.get(code, {})
    revenue = -(mv(4000).get("debit", 0) - mv(4000).get("credit", 0)) if 4000 in by else Decimal("0")
    direct = (mv(5000).get("debit", 0) - mv(5000).get("credit", 0)) if 5000 in by else Decimal("0")
    expenses = sum(((by[c]["debit"] - by[c]["credit"]) for c in (6000, 7000, 8000, 10000) if c in by), Decimal("0"))
    other_inc = (by[9000]["credit"] - by[9000]["debit"]) if 9000 in by else Decimal("0")
    kpis = [("الإيرادات", revenue), ("التكاليف المباشرة", direct), ("مجمل الربح", revenue - direct), ("صافي الربح", revenue - direct - expenses + other_inc),
            ("رصيد الخزينة", g(1101)), ("رصيد البنوك", g(1102)), ("المدينون (العملاء)", g(1201)), ("الدائنون", -(g(2101) + g(2102)))]
    counts = dict(
        customers=db.scalar(select(func.count()).select_from(Customer).where(Customer.company_id == company.id)),
        suppliers=db.scalar(select(func.count()).select_from(Supplier).where(Supplier.company_id == company.id)),
        projects=db.scalar(select(func.count()).select_from(Project).where(Project.company_id == company.id, Project.status == "جاري")),
        drafts=db.scalar(select(func.count()).select_from(JournalEntry).where(JournalEntry.company_id == company.id, JournalEntry.status == "draft")),
        entries=db.scalar(select(func.count()).select_from(JournalEntry).where(JournalEntry.company_id == company.id, JournalEntry.status == "posted")))
    return render(request, "dashboard.html", user, kpis=kpis, tot=tot, start=start, end=end, counts=counts)


# ------------------------------------------------------------------ generic master-data CRUD
class F:
    def __init__(self, name, label, kind="text", req=False, choices=None, create_only=False, hint=""):
        self.name, self.label, self.kind, self.req, self.choices, self.create_only, self.hint = name, label, kind, req, choices, create_only, hint


def _cust_choices(db, cid):
    return [(c.id, f"{c.code} - {c.name}") for c in db.scalars(select(Customer).where(Customer.company_id == cid).order_by(Customer.code))]


def _cc_choices(db, cid):
    return [(c.id, f"{c.code} - {c.name}") for c in db.scalars(select(CostCenter).where(CostCenter.company_id == cid).order_by(CostCenter.code))]


PARTY = [F("code", "الكود", req=True), F("name", "الاسم", req=True), F("phone", "الهاتف"), F("address", "العنوان"), F("tax_no", "الرقم الضريبي"),
         F("contact", "الشخص المسؤول"), F("payment_terms", "مدة السداد (يوم)", "int"), F("opening_balance", "الرصيد الافتتاحي", "money"), F("active", "نشط", "check")]
MASTERS = {
    "customers": dict(model=Customer, title="العملاء", cols=[("code", "الكود"), ("name", "الاسم"), ("phone", "الهاتف"), ("payment_terms", "مدة السداد"), ("credit_limit", "حد الائتمان", "money"), ("opening_balance", "رصيد افتتاحي", "money")],
                      fields=PARTY[:7] + [F("credit_limit", "حد الائتمان", "money")] + PARTY[7:], order="code"),
    "suppliers": dict(model=Supplier, title="الموردون والمقاولون", cols=[("code", "الكود"), ("name", "الاسم"), ("kind", "النوع"), ("phone", "الهاتف"), ("payment_terms", "مدة السداد"), ("opening_balance", "رصيد افتتاحي", "money")],
                      fields=PARTY[:2] + [F("kind", "النوع", "select", True, ["مورد", "مقاول باطن"])] + PARTY[2:], order="code"),
    "cost-centers": dict(model=CostCenter, title="مراكز التكلفة", cols=[("code", "الكود"), ("name", "الاسم"), ("kind", "النوع")],
                         fields=[F("code", "الكود", req=True), F("name", "الاسم", req=True), F("kind", "النوع", "select", True, ["مشروع", "مكتب رئيسي", "إدارة", "موقع", "نشاط"]), F("active", "نشط", "check")], order="code"),
    "projects": dict(model=Project, title="المشاريع", cols=[("code", "الكود"), ("name", "الاسم"), ("customer", "العميل", "rel"), ("contract_value", "قيمة العقد", "money"), ("status", "الحالة")],
                     fields=[F("code", "الكود", req=True), F("name", "اسم المشروع", req=True), F("customer_id", "العميل", "select", False, _cust_choices), F("contract_no", "رقم العقد"),
                             F("contract_date", "تاريخ العقد", "date"), F("start_date", "تاريخ البدء", "date"), F("end_date", "تاريخ الانتهاء المتوقع", "date"),
                             F("contract_value", "قيمة العقد", "money"), F("status", "الحالة", "select", True, PROJECT_STATUS), F("manager", "مدير المشروع"), F("location", "الموقع"),
                             F("cost_center_id", "مركز التكلفة", "select", False, _cc_choices), F("retention_pct", "نسبة الضمان %", "pct"), F("advance_pct", "نسبة الدفعة المقدمة %", "pct"), F("notes", "ملاحظات", "textarea")], order="code"),
    "accounts": dict(model=Account, title="دليل الحسابات", cols=[("code", "الكود"), ("name", "الاسم"), ("type_name", "النوع", "prop"), ("level", "المستوى", "prop"), ("nature", "الطبيعة", "prop")],
                     fields=[F("code", "كود الحساب", "int", True, create_only=True, hint="المستوى 1 ينتهي بـ 000، المستوى 2 بـ 00، حسابات الحركة غير ذلك"), F("name", "اسم الحساب", req=True),
                             F("active", "نشط", "check"), F("project_required", "يتطلب مشروع", "check"), F("cc_required", "يتطلب مركز تكلفة", "check")], order="code"),
}


def master_cfg(key):
    if key not in MASTERS:
        raise HTTPException(404)
    return MASTERS[key]


def parse_fields(cfg, form, db, cid, obj=None):
    vals, errs = {}, []
    for f in cfg["fields"]:
        if f.create_only and obj is not None:
            continue
        raw = form.get(f.name)
        try:
            if f.kind == "check":
                vals[f.name] = form.get(f.name) == "on"; continue
            raw = (raw or "").strip()
            if raw == "":
                if f.req:
                    errs.append(f"الحقل «{f.label}» مطلوب")
                vals[f.name] = None if f.kind in ("date", "select") else (Decimal("0") if f.kind in ("money", "pct") else (0 if f.kind == "int" else ""))
                if f.kind == "select" and f.req is False:
                    vals[f.name] = None
                continue
            if f.kind == "int":
                vals[f.name] = int(raw.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
            elif f.kind == "money":
                vals[f.name] = D(raw)
            elif f.kind == "pct":
                vals[f.name] = (D(raw) / 100).quantize(Decimal("0.0001"))
            elif f.kind == "date":
                vals[f.name] = dt.date.fromisoformat(raw)
            elif f.kind == "select":
                ch = f.choices(db, cid) if callable(f.choices) else f.choices
                valid = {str(v if isinstance(v, (str, int)) else v) for v in (c[0] if isinstance(c, tuple) else c for c in ch)}
                if raw not in valid:
                    errs.append(f"قيمة غير صحيحة في «{f.label}»")
                vals[f.name] = int(raw) if callable(f.choices) else raw
            else:
                vals[f.name] = raw
        except (ValueError, TypeError):
            errs.append(f"قيمة غير صالحة في «{f.label}»")
    return vals, errs


def form_context(cfg, db, cid):
    out = []
    for f in cfg["fields"]:
        ch = f.choices(db, cid) if callable(f.choices) else ([(c, c) for c in f.choices] if f.choices else None)
        out.append((f, ch))
    return out


@app.get("/m/{key}", response_class=HTMLResponse)
def master_list(key: str, request: Request, q: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    cfg = master_cfg(key); M = cfg["model"]
    stmt = select(M).where(M.company_id == user.company_id).order_by(getattr(M, cfg["order"]))
    if q:
        stmt = stmt.where(or_(M.name.ilike(f"%{q}%"), *( [M.code.ilike(f"%{q}%")] if M is not Account else [])))
    items = db.scalars(stmt.limit(500)).all()
    return render(request, "master_list.html", user, key=key, cfg=cfg, items=items, q=q)


@app.get("/m/{key}/new", response_class=HTMLResponse)
def master_new(key: str, request: Request, user: User = Depends(need_write), db: Session = Depends(get_db)):
    cfg = master_cfg(key)
    return render(request, "master_form.html", user, key=key, cfg=cfg, fields=form_context(cfg, db, user.company_id), obj=None, vals={"active": True, "status": "جديد", "kind": None, "payment_terms": 30}, errors=[])


def _to_view(vals, obj=None):
    return vals


@app.post("/m/{key}/new")
async def master_create(key: str, request: Request, user: User = Depends(need_write), db: Session = Depends(get_db)):
    cfg = master_cfg(key); M = cfg["model"]
    form = await get_form(request)
    vals, errs = parse_fields(cfg, form, db, user.company_id)
    if M is Account and not errs:
        code = vals["code"]
        if code < 1000 or code > 10999:
            errs.append("كود الحساب يجب أن يكون بين 1000 و10999")
        else:
            tmp = Account(code=code)
            if tmp.level > 1 and not db.scalar(select(Account.id).where(Account.company_id == user.company_id, Account.code == tmp.parent_code)):
                errs.append(f"الحساب الأب {tmp.parent_code} غير موجود - أضفه أولاً")
    if errs:
        return render(request, "master_form.html", user, key=key, cfg=cfg, fields=form_context(cfg, db, user.company_id), obj=None, vals=dict(form), errors=errs)
    obj = M(company_id=user.company_id, **vals)
    db.add(obj)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return render(request, "master_form.html", user, key=key, cfg=cfg, fields=form_context(cfg, db, user.company_id), obj=None, vals=dict(form), errors=["الكود مستخدم من قبل - اختر كوداً فريداً"])
    audit(db, user, "create", key, obj.id, str(vals.get("code", "")))
    db.commit(); flash(request, "تم الحفظ")
    return RedirectResponse(f"/m/{key}", status_code=303)


def _get_obj(db, cfg, id, user):
    obj = db.get(cfg["model"], id)
    if not obj or obj.company_id != user.company_id:
        raise HTTPException(404)
    return obj


@app.get("/m/{key}/{id}", response_class=HTMLResponse)
def master_edit(key: str, id: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    cfg = master_cfg(key); obj = _get_obj(db, cfg, id, user)
    vals = {f.name: getattr(obj, f.name) for f in cfg["fields"]}
    for f in cfg["fields"]:
        if f.kind == "pct" and vals[f.name] is not None:
            vals[f.name] = f"{Decimal(vals[f.name]) * 100:.2f}".rstrip("0").rstrip(".")
    return render(request, "master_form.html", user, key=key, cfg=cfg, fields=form_context(cfg, db, user.company_id), obj=obj, vals=vals, errors=[])


@app.post("/m/{key}/{id}")
async def master_update(key: str, id: int, request: Request, user: User = Depends(need_write), db: Session = Depends(get_db)):
    cfg = master_cfg(key); obj = _get_obj(db, cfg, id, user)
    form = await get_form(request)
    vals, errs = parse_fields(cfg, form, db, user.company_id, obj)
    if errs:
        return render(request, "master_form.html", user, key=key, cfg=cfg, fields=form_context(cfg, db, user.company_id), obj=obj, vals=dict(form), errors=errs)
    for k, v in vals.items():
        setattr(obj, k, v)
    audit(db, user, "update", key, obj.id, getattr(obj, "name", ""))
    db.commit(); flash(request, "تم التعديل")
    return RedirectResponse(f"/m/{key}", status_code=303)


# ------------------------------------------------------------------ journal
def _acct_choices(db, cid):
    return db.scalars(select(Account).where(Account.company_id == cid, Account.active == True).order_by(Account.code)).all()  # noqa: E712


def _entry_form_ctx(db, cid):
    return dict(accounts=[a for a in _acct_choices(db, cid) if a.level == 3],
                projects=db.scalars(select(Project).where(Project.company_id == cid).order_by(Project.code)).all(),
                ccs=db.scalars(select(CostCenter).where(CostCenter.company_id == cid, CostCenter.active == True).order_by(CostCenter.code)).all())  # noqa: E712


def _read_lines(form, cid):
    ids = form.getlist("account_id"); drs = form.getlist("debit"); crs = form.getlist("credit"); ds = form.getlist("line_desc")
    ps = form.getlist("project_id"); cs = form.getlist("cc_id")
    lines, errs = [], []
    for i in range(len(ids)):
        if not (ids[i] or (drs[i] or "").strip() or (crs[i] or "").strip()):
            continue
        try:
            if not ids[i]:
                raise ValueError("اختر الحساب")
            lines.append(JournalLine(company_id=cid, account_id=int(ids[i]), debit=D(drs[i]), credit=D(crs[i]), description=(ds[i] or "").strip(),
                                     project_id=int(ps[i]) if ps[i] else None, cost_center_id=int(cs[i]) if cs[i] else None))
        except ValueError as e:
            errs.append(f"سطر {i + 1}: {e}")
    return lines, errs


@app.get("/journal", response_class=HTMLResponse)
def journal_list(request: Request, q: str = "", status: str = "", start: str = "", end: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(JournalEntry).where(JournalEntry.company_id == user.company_id)
    if q:
        stmt = stmt.where(or_(JournalEntry.number.ilike(f"%{q}%"), JournalEntry.description.ilike(f"%{q}%"), JournalEntry.reference.ilike(f"%{q}%")))
    if status in ("draft", "posted"):
        stmt = stmt.where(JournalEntry.status == status)
    if start:
        stmt = stmt.where(JournalEntry.date >= pdate(start, dt.date.min))
    if end:
        stmt = stmt.where(JournalEntry.date <= pdate(end, dt.date.max))
    items = db.scalars(stmt.order_by(JournalEntry.date.desc(), JournalEntry.id.desc()).limit(300)).all()
    return render(request, "journal_list.html", user, items=items, q=q, status=status, start=start, end=end)


@app.get("/journal/new", response_class=HTMLResponse)
def journal_new(request: Request, user: User = Depends(need_write), db: Session = Depends(get_db)):
    c = user.company
    d = min(max(dt.date.today(), c.fy_start), c.fy_end)
    return render(request, "journal_form.html", user, entry=None, number=next_number(db, c.id), date=d, desc="", ref="", lines=[], errors=[], **_entry_form_ctx(db, c.id))


async def _save_entry(request, db, user, entry=None):
    form = await get_form(request)
    c = user.company
    errs = []
    date = pdate(form.get("date"), None)
    if not date:
        errs.append("التاريخ غير صالح")
    lines, lerrs = _read_lines(form, c.id)
    errs += lerrs
    action = form.get("action", "save")
    if errs:
        return None, errs, form
    if entry is None:
        entry = JournalEntry(company_id=c.id, number=next_number(db, c.id), date=date, status="draft", created_by=user.id)
        db.add(entry)
    entry.date = date; entry.description = (form.get("description") or "").strip(); entry.reference = (form.get("reference") or "").strip()
    entry.lines.clear(); db.flush()
    for l in lines:
        entry.lines.append(l)
    db.flush()
    if action == "post":
        try:
            post_entry(db, entry, c, user)
        except ValueError as e:
            db.commit()  # keep as draft so the user doesn't lose the data
            return entry, [str(e)], form
    audit(db, user, "save", "journal", entry.number)
    db.commit()
    return entry, [], form


@app.post("/journal/new")
async def journal_create(request: Request, user: User = Depends(need_write), db: Session = Depends(get_db)):
    entry, errs, form = await _save_entry(request, db, user)
    if entry is None or errs:
        if entry is not None:
            flash(request, "حُفظ كمسودة لكن لم يُرحَّل: " + "؛ ".join(errs), "err")
            return RedirectResponse(f"/journal/{entry.id}/edit", status_code=303)
        c = user.company
        return render(request, "journal_form.html", user, entry=None, number=next_number(db, c.id), date=pdate(form.get("date"), dt.date.today()), desc=form.get("description", ""), ref=form.get("reference", ""),
                      lines=[], form_lines=list(zip(form.getlist("account_id"), form.getlist("debit"), form.getlist("credit"), form.getlist("line_desc"), form.getlist("project_id"), form.getlist("cc_id"))), errors=errs, **_entry_form_ctx(db, c.id))
    flash(request, f"تم {'الترحيل' if entry.status == 'posted' else 'الحفظ كمسودة'} - {entry.number}")
    return RedirectResponse(f"/journal/{entry.id}", status_code=303)


def _entry(db, id, user) -> JournalEntry:
    e = db.get(JournalEntry, id)
    if not e or e.company_id != user.company_id:
        raise HTTPException(404)
    return e


@app.get("/journal/{id}", response_class=HTMLResponse)
def journal_view(id: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    e = _entry(db, id, user)
    errs = validate_entry(db, e, user.company) if e.status == "draft" else []
    rev = db.scalar(select(JournalEntry).where(JournalEntry.reversal_of_id == e.id))
    return render(request, "journal_view.html", user, e=e, errs=errs, rev=rev)


@app.get("/journal/{id}/edit", response_class=HTMLResponse)
def journal_edit(id: int, request: Request, user: User = Depends(need_write), db: Session = Depends(get_db)):
    e = _entry(db, id, user)
    if e.status != "draft":
        raise HTTPException(400, "لا يمكن تعديل قيد مرحّل - استخدم عكس القيد")
    fl = [(str(l.account_id), str(l.debit or ""), str(l.credit or ""), l.description, str(l.project_id or ""), str(l.cost_center_id or "")) for l in e.lines]
    return render(request, "journal_form.html", user, entry=e, number=e.number, date=e.date, desc=e.description, ref=e.reference, lines=[], form_lines=fl, errors=[], **_entry_form_ctx(db, e.company_id))


@app.post("/journal/{id}/edit")
async def journal_update(id: int, request: Request, user: User = Depends(need_write), db: Session = Depends(get_db)):
    e = _entry(db, id, user)
    if e.status != "draft":
        raise HTTPException(400, "لا يمكن تعديل قيد مرحّل")
    entry, errs, form = await _save_entry(request, db, user, e)
    if entry is None:
        fl = list(zip(form.getlist("account_id"), form.getlist("debit"), form.getlist("credit"), form.getlist("line_desc"), form.getlist("project_id"), form.getlist("cc_id")))
        return render(request, "journal_form.html", user, entry=e, number=e.number, date=pdate(form.get("date"), e.date), desc=form.get("description", ""), ref=form.get("reference", ""), lines=[], form_lines=fl, errors=errs, **_entry_form_ctx(db, e.company_id))
    if errs:
        flash(request, "حُفظ كمسودة لكن لم يُرحَّل: " + "؛ ".join(errs), "err")
        return RedirectResponse(f"/journal/{e.id}/edit", status_code=303)
    flash(request, f"تم {'الترحيل' if e.status == 'posted' else 'الحفظ'} - {e.number}")
    return RedirectResponse(f"/journal/{e.id}", status_code=303)


@app.post("/journal/{id}/post")
async def journal_post(id: int, request: Request, user: User = Depends(need_write), db: Session = Depends(get_db)):
    await get_form(request)
    e = _entry(db, id, user)
    try:
        post_entry(db, e, user.company, user); db.commit(); flash(request, "تم الترحيل")
    except ValueError as ex:
        db.rollback(); flash(request, "لم يُرحَّل: " + str(ex), "err")
    return RedirectResponse(f"/journal/{id}", status_code=303)


@app.post("/journal/{id}/delete")
async def journal_delete(id: int, request: Request, user: User = Depends(need_write), db: Session = Depends(get_db)):
    await get_form(request)
    e = _entry(db, id, user)
    if e.status != "draft":
        flash(request, "لا يمكن حذف قيد مرحّل - استخدم عكس القيد", "err")
        return RedirectResponse(f"/journal/{id}", status_code=303)
    audit(db, user, "delete", "journal", e.number); db.delete(e); db.commit(); flash(request, "تم حذف المسودة")
    return RedirectResponse("/journal", status_code=303)


@app.post("/journal/{id}/reverse")
async def journal_reverse(id: int, request: Request, user: User = Depends(need_write), db: Session = Depends(get_db)):
    form = await get_form(request)
    e = _entry(db, id, user)
    try:
        rev = reverse_entry(db, e, user.company, user, pdate(form.get("date"), e.date)); db.commit(); flash(request, f"تم عكس القيد بقيد جديد {rev.number}")
        return RedirectResponse(f"/journal/{rev.id}", status_code=303)
    except ValueError as ex:
        db.rollback(); flash(request, str(ex), "err")
        return RedirectResponse(f"/journal/{id}", status_code=303)


# ------------------------------------------------------------------ reports
@app.get("/reports/trial-balance", response_class=HTMLResponse)
def tb_page(request: Request, start: str = "", end: str = "", hide_groups: int = 0, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = user.company; ds, de = default_period(c)
    s, e = pdate(start, ds), pdate(end, de)
    rows, tot = trial_balance(db, c.id, s, e)
    if hide_groups:
        rows = [r for r in rows if r["level"] == 3]
    return render(request, "trial_balance.html", user, rows=rows, tot=tot, start=s, end=e, hide_groups=hide_groups)


@app.get("/reports/ledger", response_class=HTMLResponse)
def ledger_page(request: Request, account: int = 0, start: str = "", end: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = user.company; ds, de = default_period(c)
    s, e = pdate(start, ds), pdate(end, de)
    accounts = db.scalars(select(Account).where(Account.company_id == c.id).order_by(Account.code)).all()
    acc = next((a for a in accounts if a.id == account), None)
    opening, rows, closing = (Decimal("0"), [], Decimal("0"))
    if acc:
        opening, rows, closing = ledger(db, c.id, acc, s, e)
    return render(request, "ledger.html", user, accounts=accounts, acc=acc, opening=opening, rows=rows, closing=closing, start=s, end=e)


# ------------------------------------------------------------------ admin
@app.get("/admin/users", response_class=HTMLResponse)
def users_page(request: Request, user: User = Depends(need_admin), db: Session = Depends(get_db)):
    users = db.scalars(select(User).where(User.company_id == user.company_id).order_by(User.id)).all()
    return render(request, "users.html", user, users=users, errors=[])


@app.post("/admin/users")
async def users_create(request: Request, user: User = Depends(need_admin), db: Session = Depends(get_db)):
    form = await get_form(request)
    action = form.get("action")
    if action == "create":
        u, p, r = (form.get("username") or "").strip(), form.get("password") or "", form.get("role")
        errs = []
        if not u:
            errs.append("اسم المستخدم مطلوب")
        if len(p) < 8:
            errs.append("كلمة المرور يجب ألا تقل عن 8 خانات")
        if r not in ROLES:
            errs.append("الصلاحية غير صحيحة")
        if db.scalar(select(User).where(User.username == u)):
            errs.append("اسم المستخدم مستخدم من قبل")
        if errs:
            users = db.scalars(select(User).where(User.company_id == user.company_id)).all()
            return render(request, "users.html", user, users=users, errors=errs)
        nu = User(company_id=user.company_id, username=u, full_name=(form.get("full_name") or "").strip(), password_hash=hash_password(p), role=r)
        db.add(nu); db.flush(); audit(db, user, "create", "user", nu.id, u); flash(request, "تم إنشاء المستخدم")
    else:
        t = db.get(User, int(form.get("user_id") or 0))
        if t and t.company_id == user.company_id:
            if action == "toggle" and t.id != user.id:
                t.is_active = not t.is_active; audit(db, user, "toggle", "user", t.id, str(t.is_active))
            elif action == "reset" and len(form.get("password") or "") >= 8:
                t.password_hash = hash_password(form.get("password")); audit(db, user, "reset_password", "user", t.id); flash(request, "تم تغيير كلمة المرور")
            elif action == "role" and form.get("role") in ROLES and t.id != user.id:
                t.role = form.get("role"); audit(db, user, "role", "user", t.id, t.role)
    db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@app.get("/admin/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: User = Depends(need_admin)):
    return render(request, "settings.html", user, errors=[])


@app.post("/admin/settings")
async def settings_save(request: Request, user: User = Depends(need_admin), db: Session = Depends(get_db)):
    form = await get_form(request)
    c = db.get(Company, user.company_id)
    fy = pdate(form.get("fy_start"), None)
    if not fy or not (form.get("name") or "").strip():
        return render(request, "settings.html", user, errors=["الاسم وبداية السنة المالية مطلوبان"])
    c.name = form.get("name").strip(); c.activity = form.get("activity", ""); c.address = form.get("address", ""); c.phone = form.get("phone", "")
    c.tax_no = form.get("tax_no", ""); c.currency = form.get("currency", "") or "جنيه مصري"; c.fy_start = fy
    audit(db, user, "update", "company", c.id); db.commit(); flash(request, "تم حفظ الإعدادات")
    return RedirectResponse("/admin/settings", status_code=303)


@app.get("/admin/audit", response_class=HTMLResponse)
def audit_page(request: Request, user: User = Depends(need_admin), db: Session = Depends(get_db)):
    logs = db.scalars(select(AuditLog).where(AuditLog.company_id == user.company_id).order_by(AuditLog.id.desc()).limit(300)).all()
    return render(request, "audit.html", user, logs=logs)
