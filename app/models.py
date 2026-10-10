"""Modello dati. Gli importi sono sempre in centesimi (int) con segno:
positivo = entrata, negativo = uscita."""
from datetime import date, datetime
from pathlib import PurePath

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

ACCOUNT_KINDS = {"conto": "Conto corrente", "carta": "Carta di credito / prepagata", "cassa": "Cassa contanti"}


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    bank: Mapped[str] = mapped_column(String(100), default="")
    kind: Mapped[str] = mapped_column(String(20), default="conto")
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    iban: Mapped[str] = mapped_column(String(40), default="")
    include_in_forecast: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    color: Mapped[str] = mapped_column(String(7), default="#2a78d6")
    csv_profile: Mapped[str] = mapped_column(Text, default="")  # JSON mappatura colonne
    notes: Mapped[str] = mapped_column(Text, default="")
    # Collegamento Open Banking (Enable Banking): uid del conto nella sessione PSD2
    connection_id: Mapped[int | None] = mapped_column(
        ForeignKey("bank_connections.id", ondelete="SET NULL"), nullable=True
    )
    external_uid: Mapped[str] = mapped_column(String(100), default="")

    connection: Mapped["BankConnection | None"] = relationship(back_populates="accounts")
    snapshots: Mapped[list["BalanceSnapshot"]] = relationship(
        back_populates="account", cascade="all, delete-orphan", order_by="BalanceSnapshot.date"
    )

    @property
    def kind_label(self) -> str:
        return ACCOUNT_KINDS.get(self.kind, self.kind)


class BankConnection(Base):
    """Consenso PSD2 verso una banca (tramite Enable Banking). Dura al massimo 90-180 giorni."""

    __tablename__ = "bank_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(20), default="enablebanking")
    aspsp_name: Mapped[str] = mapped_column(String(200))
    aspsp_country: Mapped[str] = mapped_column(String(2), default="IT")
    psu_type: Mapped[str] = mapped_column(String(10), default="business")
    status: Mapped[str] = mapped_column(String(20), default="in_attesa")  # in_attesa | attivo | scaduto | errore
    state: Mapped[str] = mapped_column(String(64), default="")  # anti-CSRF del redirect
    session_id: Mapped[str] = mapped_column(String(100), default="")
    valid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    session_accounts: Mapped[str] = mapped_column(Text, default="")  # JSON dei conti restituiti dalla banca
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")

    accounts: Mapped[list["Account"]] = relationship(back_populates="connection")


class BalanceSnapshot(Base):
    """Saldo certo di un conto a fine giornata `date` (i movimenti di quel giorno sono già inclusi)."""

    __tablename__ = "balance_snapshots"
    __table_args__ = (UniqueConstraint("account_id", "date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    date: Mapped[date] = mapped_column(Date)
    balance_cents: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(20), default="manuale")

    account: Mapped[Account] = relationship(back_populates="snapshots")


class Transaction(Base):
    """Movimento reale (da estratto conto, import CSV, API, o inserito a mano per la cassa)."""

    __tablename__ = "transactions"
    __table_args__ = (UniqueConstraint("account_id", "dedupe_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text, default="")
    counterparty: Mapped[str] = mapped_column(String(200), default="")
    category: Mapped[str] = mapped_column(String(100), default="")
    source: Mapped[str] = mapped_column(String(20), default="manuale")
    dedupe_key: Mapped[str] = mapped_column(String(64))
    import_batch: Mapped[str] = mapped_column(String(40), default="")
    planned_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("planned_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    account: Mapped[Account] = relationship()
    planned_item: Mapped["PlannedItem | None"] = relationship(back_populates="transactions")


class Recurrence(Base):
    """Regola che genera voci previste (stipendi, F24, affitti, abbonamenti...)."""

    __tablename__ = "recurrences"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    amount_cents: Mapped[int] = mapped_column(Integer)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    category: Mapped[str] = mapped_column(String(100), default="")
    frequency: Mapped[str] = mapped_column(String(20), default="mensile")
    day_of_month: Mapped[int] = mapped_column(Integer, default=1)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    business_day_rule: Mapped[str] = mapped_column(String(20), default="nessuna")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    account: Mapped[Account | None] = relationship()


class Invoice(Base):
    """Fattura elettronica importata (XML FatturaPA da Aruba)."""

    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    direction: Mapped[str] = mapped_column(String(10))  # attiva | passiva
    doc_type: Mapped[str] = mapped_column(String(4), default="TD01")
    number: Mapped[str] = mapped_column(String(50))
    date: Mapped[date] = mapped_column(Date)
    counterparty: Mapped[str] = mapped_column(String(200))
    counterparty_vat: Mapped[str] = mapped_column(String(30), default="")
    total_cents: Mapped[int] = mapped_column(Integer)
    file_name: Mapped[str] = mapped_column(String(255))
    file_hash: Mapped[str] = mapped_column(String(64), unique=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    planned_items: Mapped[list["PlannedItem"]] = relationship(back_populates="invoice")


class PlannedItem(Base):
    """Entrata/uscita prevista in una data."""

    __tablename__ = "planned_items"
    __table_args__ = (UniqueConstraint("recurrence_id", "occurrence_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    amount_cents: Mapped[int] = mapped_column(Integer)
    due_date: Mapped[date] = mapped_column(Date, index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    counterparty: Mapped[str] = mapped_column(String(200), default="")
    category: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(20), default="previsto")  # previsto | pagato | annullato
    paid_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="manuale")  # manuale | ricorrente | fattura
    estimated_date: Mapped[bool] = mapped_column(Boolean, default=False)
    recurrence_id: Mapped[int | None] = mapped_column(
        ForeignKey("recurrences.id", ondelete="CASCADE"), nullable=True
    )
    occurrence_key: Mapped[date | None] = mapped_column(Date, nullable=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    account: Mapped[Account | None] = relationship()
    invoice: Mapped[Invoice | None] = relationship(back_populates="planned_items")
    transactions: Mapped[list[Transaction]] = relationship(back_populates="planned_item")


class DocCategory(Base):
    """Tipologia di documento da consegnare al commercialista."""

    __tablename__ = "doc_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    frequency: Mapped[str] = mapped_column(String(20))  # mensile | trimestrale
    due_day: Mapped[int] = mapped_column(Integer, default=6)
    # mesi dopo la fine del periodo (1 = mese successivo)
    due_month_offset: Mapped[int] = mapped_column(Integer, default=1)
    auto_export: Mapped[str] = mapped_column(String(20), default="")  # conto | carta | cassa | ""
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ChecklistItem(Base):
    """Una tipologia di documento per un periodo (es. 'Lista movimenti banche' di 2026-09)."""

    __tablename__ = "checklist_items"
    __table_args__ = (UniqueConstraint("category_id", "period_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("doc_categories.id", ondelete="CASCADE"))
    period_key: Mapped[str] = mapped_column(String(10))  # 2026-09 | 2026-Q3
    period_end: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="da_fare")
    notes: Mapped[str] = mapped_column(Text, default="")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    category: Mapped[DocCategory] = relationship()
    documents: Mapped[list["Document"]] = relationship(
        back_populates="checklist_item", cascade="all, delete-orphan", order_by="Document.uploaded_at"
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    checklist_item_id: Mapped[int] = mapped_column(ForeignKey("checklist_items.id", ondelete="CASCADE"))
    original_name: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(Text)  # relativo alla cartella documenti
    size: Mapped[int] = mapped_column(Integer, default=0)
    doc_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    supplier: Mapped[str] = mapped_column(String(200), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    generated: Mapped[bool] = mapped_column(Boolean, default=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    checklist_item: Mapped[ChecklistItem] = relationship(back_populates="documents")

    @property
    def file_name(self) -> str:
        return PurePath(self.stored_path.replace("\\", "/")).name


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
