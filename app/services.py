"""Accounting rules. Everything that protects accounting integrity lives here (not in the UI)."""
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from sqlalchemy import select, func, and_
from sqlalchemy.orm import Session
from .models import (utcnow, Account, AuditLog, Company, CostCenter, JournalEntry, JournalLine, Project, User)

ZERO = Decimal("0.00")
CASH_HINT = (1101, 1102)


def D(v) -> Decimal:
    """Parse user money input safely (accepts Arabic-Indic digits and thousands separators)."""
    if v is None or str(v).strip() == "":
        return ZERO
    s = str(v).strip().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬،", "0123456789.,,")).replace(",", "").replace(" ", "")
    try:
        return Decimal(s).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise ValueError(f"رقم غير صالح: {v}")


def audit(db: Session, user: User | None, action: str, entity: str, entity_id="", detail=""):
    db.add(AuditLog(company_id=user.company_id if user else 0, user_id=user.id if user else None,
                    username=user.username if user else "system", action=action, entity=entity, entity_id=str(entity_id), detail=detail))


def next_number(db: Session, company_id: int, prefix="JV-") -> str:
    nums = db.scalars(select(JournalEntry.number).where(JournalEntry.company_id == company_id, JournalEntry.number.like(prefix + "%"))).all()
    mx = 0
    for n in nums:
        try:
            mx = max(mx, int(n[len(prefix):]))
        except ValueError:
            pass
    return f"{prefix}{mx + 1:04d}"


def validate_entry(db: Session, entry: JournalEntry, company: Company) -> list[str]:
    """Return list of human-readable errors; empty list => entry may be posted."""
    errs = []
    if not (company.fy_start <= entry.date <= company.fy_end):
        errs.append(f"التاريخ خارج السنة المالية ({company.fy_start} إلى {company.fy_end})")
    if len(entry.lines) < 2:
        errs.append("القيد يجب أن يحتوي على سطرين على الأقل")
    for i, l in enumerate(entry.lines, 1):
        acc = db.get(Account, l.account_id)
        d, c = l.debit or ZERO, l.credit or ZERO
        if acc is None or acc.company_id != entry.company_id:
            errs.append(f"سطر {i}: حساب غير موجود"); continue
        if d < 0 or c < 0:
            errs.append(f"سطر {i}: مبلغ سالب غير مسموح")
        if d > 0 and c > 0:
            errs.append(f"سطر {i}: لا يجوز مدين ودائن معاً في نفس السطر")
        if d == 0 and c == 0:
            errs.append(f"سطر {i}: السطر بدون مبلغ")
        if not acc.active:
            errs.append(f"سطر {i}: الحساب {acc.code} موقوف")
        if acc.level < 3:
            errs.append(f"سطر {i}: الحساب {acc.code} حساب رئيسي لا يقبل الحركة")
        if acc.project_required and not l.project_id:
            errs.append(f"سطر {i}: الحساب {acc.code} يتطلب كود مشروع")
        if acc.cc_required and not (l.cost_center_id or l.project_id):
            errs.append(f"سطر {i}: الحساب {acc.code} يتطلب مركز تكلفة أو مشروعاً")
        if l.project_id:
            p = db.get(Project, l.project_id)
            if p is None or p.company_id != entry.company_id:
                errs.append(f"سطر {i}: مشروع غير موجود")
    if entry.total_debit != entry.total_credit:
        errs.append(f"القيد غير متوازن: مدين {entry.total_debit} ≠ دائن {entry.total_credit}")
    return errs


def post_entry(db: Session, entry: JournalEntry, company: Company, user: User):
    if entry.status == "posted":
        raise ValueError("القيد مرحّل بالفعل")
    errs = validate_entry(db, entry, company)
    if errs:
        raise ValueError("؛ ".join(errs))
    # default cost center from the project (same rule as the Excel system)
    for l in entry.lines:
        if l.project_id and not l.cost_center_id:
            p = db.get(Project, l.project_id)
            l.cost_center_id = p.cost_center_id if p else None
    entry.status = "posted"
    entry.posted_by = user.id
    entry.posted_at = utcnow()
    audit(db, user, "post", "journal", entry.number, f"مدين={entry.total_debit}")


def reverse_entry(db: Session, entry: JournalEntry, company: Company, user: User, date: dt.date | None = None) -> JournalEntry:
    if entry.status != "posted":
        raise ValueError("يمكن عكس القيود المرحّلة فقط")
    if db.scalar(select(func.count()).select_from(JournalEntry).where(JournalEntry.reversal_of_id == entry.id)):
        raise ValueError("هذا القيد تم عكسه من قبل")
    rev = JournalEntry(company_id=entry.company_id, number=next_number(db, entry.company_id), date=date or entry.date,
                       description=f"عكس القيد {entry.number}: {entry.description}", reference=entry.number, status="draft",
                       source="reversal", reversal_of_id=entry.id, created_by=user.id)
    for l in entry.lines:
        rev.lines.append(JournalLine(company_id=l.company_id, account_id=l.account_id, debit=l.credit, credit=l.debit, description=l.description,
                                     project_id=l.project_id, cost_center_id=l.cost_center_id, customer_id=l.customer_id, supplier_id=l.supplier_id))
    db.add(rev); db.flush()
    post_entry(db, rev, company, user)
    audit(db, user, "reverse", "journal", entry.number, f"قيد العكس {rev.number}")
    return rev


def _line_filter(company_id, start=None, end=None):
    conds = [JournalLine.company_id == company_id, JournalEntry.status == "posted"]
    if start:
        conds.append(JournalEntry.date >= start)
    if end:
        conds.append(JournalEntry.date <= end)
    return conds


def trial_balance(db: Session, company_id: int, start: dt.date, end: dt.date):
    """Return (rows, totals). Posted entries only. Group accounts are rolled up from leaf accounts."""
    accounts = db.scalars(select(Account).where(Account.company_id == company_id).order_by(Account.code)).all()
    q_open = (select(JournalLine.account_id, func.coalesce(func.sum(JournalLine.debit - JournalLine.credit), 0))
              .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
              .where(*_line_filter(company_id, None, start - dt.timedelta(days=1))).group_by(JournalLine.account_id))
    q_mov = (select(JournalLine.account_id, func.coalesce(func.sum(JournalLine.debit), 0), func.coalesce(func.sum(JournalLine.credit), 0))
             .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
             .where(*_line_filter(company_id, start, end)).group_by(JournalLine.account_id))
    opening = {a: Decimal(v) for a, v in db.execute(q_open)}
    mov = {a: (Decimal(d), Decimal(c)) for a, d, c in db.execute(q_mov)}
    leaf = {}
    for a in accounts:
        d, c = mov.get(a.id, (ZERO, ZERO))
        leaf[a.id] = dict(code=a.code, opening=opening.get(a.id, ZERO), debit=d, credit=c)
    rows = []
    for a in accounts:
        if a.level == 3:
            r = leaf[a.id]
        else:
            members = [leaf[x.id] for x in accounts if x.level == 3 and a.code <= x.code <= a.range_hi]
            r = dict(code=a.code, opening=sum((m["opening"] for m in members), ZERO), debit=sum((m["debit"] for m in members), ZERO),
                     credit=sum((m["credit"] for m in members), ZERO))
        r = dict(r); r["closing"] = r["opening"] + r["debit"] - r["credit"]
        r.update(account=a, level=a.level)
        if any(abs(r[k]) > 0 for k in ("opening", "debit", "credit")) or a.level < 3:
            rows.append(r)
    leaves = [r for r in rows if r["level"] == 3]
    tot = dict(open_dr=sum((r["opening"] for r in leaves if r["opening"] > 0), ZERO), open_cr=-sum((r["opening"] for r in leaves if r["opening"] < 0), ZERO),
               debit=sum((r["debit"] for r in leaves), ZERO), credit=sum((r["credit"] for r in leaves), ZERO),
               close_dr=sum((r["closing"] for r in leaves if r["closing"] > 0), ZERO), close_cr=-sum((r["closing"] for r in leaves if r["closing"] < 0), ZERO))
    tot["balanced"] = tot["open_dr"] == tot["open_cr"] and tot["debit"] == tot["credit"] and tot["close_dr"] == tot["close_cr"]
    return rows, tot


def ledger(db: Session, company_id: int, account: Account, start: dt.date, end: dt.date):
    lo, hi = account.code, account.range_hi
    base = (select(JournalLine).join(JournalEntry, JournalEntry.id == JournalLine.entry_id).join(Account, Account.id == JournalLine.account_id)
            .where(Account.company_id == company_id, Account.code >= lo, Account.code <= hi))
    opening = db.scalar(select(func.coalesce(func.sum(JournalLine.debit - JournalLine.credit), 0))
                        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id).join(Account, Account.id == JournalLine.account_id)
                        .where(Account.company_id == company_id, Account.code >= lo, Account.code <= hi, JournalEntry.status == "posted",
                               JournalEntry.date < start))
    lines = db.scalars(base.where(JournalEntry.status == "posted", JournalEntry.date >= start, JournalEntry.date <= end)
                       .order_by(JournalEntry.date, JournalEntry.id, JournalLine.id)).all()
    bal = Decimal(opening)
    out = []
    for l in lines:
        bal += l.debit - l.credit
        out.append(dict(line=l, entry=l.entry, balance=bal))
    return Decimal(opening), out, bal
