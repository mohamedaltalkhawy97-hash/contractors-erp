import datetime as dt


def utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

from decimal import Decimal
from sqlalchemy import (Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base

MONEY = Numeric(18, 2)
ACCOUNT_TYPES = ["أصول", "خصوم", "حقوق ملكية", "إيرادات", "تكاليف مباشرة", "تكاليف غير مباشرة", "مصروفات إدارية", "مصروفات بيع وتسويق", "إيرادات أخرى", "مصروفات أخرى"]
DEBIT_CLASSES = {1, 5, 6, 7, 8, 10}
ROLES = {"admin": "مدير النظام", "accountant": "محاسب", "viewer": "مطالعة فقط"}
PROJECT_STATUS = ["جديد", "جاري", "متوقف", "مكتمل", "مغلق"]


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    activity: Mapped[str] = mapped_column(String(200), default="")
    address: Mapped[str] = mapped_column(String(300), default="")
    phone: Mapped[str] = mapped_column(String(50), default="")
    tax_no: Mapped[str] = mapped_column(String(50), default="")
    currency: Mapped[str] = mapped_column(String(50), default="جنيه مصري")
    fy_start: Mapped[dt.date] = mapped_column(Date, default=lambda: dt.date(dt.date.today().year, 1, 1))

    @property
    def fy_end(self) -> dt.date:
        s = self.fy_start
        return dt.date(s.year + 1, s.month, s.day) - dt.timedelta(days=1)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    username: Mapped[str] = mapped_column(String(60), unique=True)
    full_name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20), default="viewer")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    company: Mapped[Company] = relationship()


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("company_id", "code"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    code: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    cc_required: Mapped[bool] = mapped_column(Boolean, default=False)
    project_required: Mapped[bool] = mapped_column(Boolean, default=False)

    @property
    def cls(self) -> int:
        return 10 if self.code >= 10000 else self.code // 1000

    @property
    def level(self) -> int:
        return 1 if self.code % 1000 == 0 else (2 if self.code % 100 == 0 else 3)

    @property
    def parent_code(self):
        return None if self.level == 1 else (self.code // 1000 * 1000 if self.level == 2 else self.code // 100 * 100)

    @property
    def nature(self) -> str:
        return "مدين" if self.cls in DEBIT_CLASSES else "دائن"

    @property
    def type_name(self) -> str:
        return ACCOUNT_TYPES[self.cls - 1] if 1 <= self.cls <= 10 else "-"

    @property
    def range_hi(self) -> int:
        return self.code + (999 if self.level == 1 else 99 if self.level == 2 else 0)


class CostCenter(Base):
    __tablename__ = "cost_centers"
    __table_args__ = (UniqueConstraint("company_id", "code"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(150))
    kind: Mapped[str] = mapped_column(String(30), default="مشروع")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Party(Base):
    __abstract__ = True
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(50), default="")
    address: Mapped[str] = mapped_column(String(300), default="")
    tax_no: Mapped[str] = mapped_column(String(50), default="")
    contact: Mapped[str] = mapped_column(String(120), default="")
    payment_terms: Mapped[int] = mapped_column(Integer, default=30)
    opening_balance: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Customer(Party):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("company_id", "code"),)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    credit_limit: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))


class Supplier(Party):
    __tablename__ = "suppliers"
    __table_args__ = (UniqueConstraint("company_id", "code"),)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="مورد")  # مورد / مقاول باطن


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("company_id", "code"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(200))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    contract_no: Mapped[str] = mapped_column(String(50), default="")
    contract_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    start_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    contract_value: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(20), default="جديد")
    manager: Mapped[str] = mapped_column(String(120), default="")
    location: Mapped[str] = mapped_column(String(200), default="")
    cost_center_id: Mapped[int | None] = mapped_column(ForeignKey("cost_centers.id"), nullable=True)
    retention_pct: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=Decimal("0.05"))
    advance_pct: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=Decimal("0"))
    notes: Mapped[str] = mapped_column(Text, default="")
    customer: Mapped[Customer | None] = relationship()
    cost_center: Mapped[CostCenter | None] = relationship()


class JournalEntry(Base):
    __tablename__ = "journal_entries"
    __table_args__ = (UniqueConstraint("company_id", "number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    number: Mapped[str] = mapped_column(String(30))
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    description: Mapped[str] = mapped_column(String(300), default="")
    reference: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(10), default="draft")  # draft | posted
    source: Mapped[str] = mapped_column(String(30), default="manual")
    reversal_of_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    posted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    posted_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    lines: Mapped[list["JournalLine"]] = relationship(back_populates="entry", cascade="all, delete-orphan", order_by="JournalLine.id")

    @property
    def total_debit(self) -> Decimal:
        return sum((l.debit or Decimal("0") for l in self.lines), Decimal("0"))

    @property
    def total_credit(self) -> Decimal:
        return sum((l.credit or Decimal("0") for l in self.lines), Decimal("0"))


class JournalLine(Base):
    __tablename__ = "journal_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entries.id"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    debit: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    credit: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    description: Mapped[str] = mapped_column(String(300), default="")
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    cost_center_id: Mapped[int | None] = mapped_column(ForeignKey("cost_centers.id"), nullable=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"), nullable=True)
    entry: Mapped[JournalEntry] = relationship(back_populates="lines")
    account: Mapped[Account] = relationship()
    project: Mapped[Project | None] = relationship()
    cost_center: Mapped[CostCenter | None] = relationship()


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    username: Mapped[str] = mapped_column(String(60), default="")
    action: Mapped[str] = mapped_column(String(40))
    entity: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(40), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
