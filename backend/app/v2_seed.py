"""Published synthetic profiles. Never resets an existing record or legacy application."""

from copy import deepcopy
from datetime import date, timedelta
from uuid import uuid4
from sqlalchemy import select
from .models import User
from .security import password_hash
from .v2_models import CreditRecord

INITIAL_TIME = "2026-10-25T09:00:00+03:00"
PROFILE_LABELS = {
    "A": "Стабильный поток · исходное предложение",
    "B": "Поставщик · альтернатива 24 месяца",
    "C": "Сезонность · ручная оценка",
    "D": "Повторные закупки · ВКЛ",
    "E": "Нет периода выписки · уточнение",
    "F": "Молодой бизнес · неприменимость",
    "G": "Отрицательный поток · экспертное решение",
    "H": "Поступления требуют классификации",
    "I": "БКИ недоступно · восстановление",
    "J": "Сроки оффера и полномочия",
    "K": "ВКЛ · конкурирующие запросы",
    "L": "ВКЛ · просрочка и закрытие",
}


def month_shift(value, months):
    import calendar

    value = date.fromisoformat(value)
    year, month = divmod(value.year * 12 + value.month - 1 + months, 12)
    return date(year, month + 1, min(value.day, calendar.monthrange(year, month + 1)[1])).isoformat()


def profile_input(letter, on="2026-10-25"):
    line = letter in {"D", "K", "L"}
    amount = 100000000 if line else 300000000 if letter == "B" else 150000000 if letter == "C" else 200000000
    cfads = 35000000 if letter == "B" else -12000000 if letter == "G" else 40000000
    receipt = 100000000 if letter == "G" else 400000000 if letter == "H" else 240000000
    old = 0 if line else 4000000 if letter == "C" else 10000000

    def period(i, historical=False):
        flow = cfads
        if letter == "C" and not historical:
            flow = [8000000, 8000000, 12000000, 18000000, 30000000, 42000000, 52000000, 52000000, 40000000, 30000000, 18000000, 10000000][
                (i - 1) % 12
            ]
        item = {
            "period": month_shift(on, i if not historical else i - 7)[:7],
            "customer_receipts_kopecks": receipt,
            "operating_payments_kopecks": receipt - flow,
            "taxes_kopecks": 0,
            "maintenance_capex_kopecks": 0,
            "mandatory_withdrawals_kopecks": 0,
            "source": "Синтетический финансовый план; среднее не является гарантией прогноза",
        }
        if not historical:
            item["old_debt_service_kopecks"] = old
        return item

    registration = month_shift(on, -3 if letter == "F" else -36 if letter == "B" else -30)
    return {
        "product_code": "DEMO_REVOLVING_V1" if line else "DEMO_TERM_LOAN_V1",
        "amount_kopecks": amount,
        "term_months": 12,
        "submitted_on": on,
        "organization": {
            "legal_form": "OOO" if letter in {"B", "D", "H", "K", "L"} else "IP",
            "registration_date": registration,
            "status": "ACTIVE",
        },
        "need": {
            "minimum_useful_kopecks": amount,
            "partial_feasible": False,
            "total_cost_kopecks": amount,
            "own_contribution_kopecks": 0,
            "purpose": "REPEATED_PURCHASES" if line else "ONE_OFF_PURCHASE",
            "description": "Повторные закупки с возвратом из продаж" if line else "Разовая закупка товарного запаса",
            "needed_on": on,
            "repayment_source": "Поступления покупателей",
            "use_pattern": "PARTS" if line else "ONCE",
        },
        "history": [period(i, True) for i in range(1, 7)],
        "forecast": [period(i) for i in range(1, 25)],
        "initial_cash_kopecks": 10000000,
        "concentration_bps": 3000,
        "active_overdue_days": 0,
        "closed_overdue_last12": False,
        "seasonal": letter == "C",
        "financials_confirmed": False,
        "documents_complete": True,
        "documents_verified": True,
        "source_checks": {"registry": "CLEAR", "credit_history": "CLEAR", "control": "CLEAR"},
        "cycle_plan": {
            "confirmed": False,
            "initial_cash_kopecks": 10000000,
            "events": [
                {"id": "target", "date": on, "kind": "TARGET_PAYMENT", "amount_kopecks": amount},
                {
                    "id": "operating",
                    "date": (date.fromisoformat(on) + timedelta(days=1)).isoformat(),
                    "kind": "OPERATING_PAYMENT",
                    "amount_kopecks": 5000000,
                },
                {
                    "id": "buyer-10",
                    "date": (date.fromisoformat(on) + timedelta(days=10)).isoformat(),
                    "kind": "CUSTOMER_RECEIPT",
                    "amount_kopecks": 20000000,
                },
                {
                    "id": "buyer-30",
                    "date": (date.fromisoformat(on) + timedelta(days=30)).isoformat(),
                    "kind": "CUSTOMER_RECEIPT",
                    "amount_kopecks": 110000000,
                },
            ],
        },
    }


def record(db, key, kind, data, organization_id=None):
    row = db.get(CreditRecord, key)
    if not row:
        row = CreditRecord(id=key, kind=kind, organization_id=organization_id, revision=1, data=deepcopy(data))
        db.add(row)
    return row


def seed_v2(db):
    record(db, "demo-clock", "clock", {"now": INITIAL_TIME, "date": "2026-10-25", "history": []})
    for letter in PROFILE_LABELS:
        oid = f"demo-org-{letter.lower()}"
        data = profile_input(letter)
        record(
            db,
            oid,
            "organization",
            {
                "id": oid,
                "synthetic_id": f"FIX-{letter}",
                "name": f"Учебный бизнес {letter}",
                "display_name": f"Учебный бизнес {letter}",
                "inn": f"99000000{ord(letter):04}",
                "registration_number": f"DEMO-{letter}-2026",
                "synthetic": True,
                **data["organization"],
                "tax_regime": "УСН",
                "scenario_id": f"FIX-{letter}",
                "baseline_old_debt_kopecks": data["forecast"][0]["old_debt_service_kopecks"],
                "baseline_old_debt_end_on": month_shift("2026-10-25", 24),
                "obligations_revision": 1,
            },
        )
        record(db, f"account:{oid}", "account", {"balance_kopecks": 10000000, "operations": []}, oid)
        email = f"fix-{letter.lower()}@demo.local"
        user = db.scalar(select(User).where(User.email == email))
        if not user:
            user = User(
                id=str(uuid4()),
                email=email,
                name=f"Подписант {letter}",
                role="entrepreneur",
                company_id=None,
                password_hash=password_hash("Demo2026!"),
            )
            db.add(user)
        record(
            db,
            f"membership:{user.id}:{oid}",
            "membership",
            {
                "user_id": user.id,
                "roles": ["CLIENT_SIGNER", "CLIENT_PREPARER"],
                "valid_from": "2026-01-01",
                "valid_to": "2026-11-02" if letter == "J" else "2029-10-25",
                "actions": ["prepare", "submit", "consent", "accept", "decline", "withdraw", "sign", "draw", "repay"],
            },
            oid,
        )
    aliases = {"good": "A", "offer": "B", "review": "C", "info": "E", "risk": "G", "retry": "I"}
    for email, letter in aliases.items():
        user = db.scalar(select(User).where(User.email == f"{email}@demo.local"))
        if user:
            oid = f"demo-org-{letter.lower()}"
            record(
                db,
                f"membership:{user.id}:{oid}",
                "membership",
                {
                    "user_id": user.id,
                    "roles": ["CLIENT_SIGNER", "CLIENT_PREPARER"],
                    "valid_from": "2026-01-01",
                    "valid_to": "2029-10-25",
                    "actions": ["prepare", "submit", "consent", "accept", "decline", "withdraw", "sign", "draw", "repay"],
                },
                oid,
            )
    # The primary demo signer can also select a distinct line organization.
    good = db.scalar(select(User).where(User.email == "good@demo.local"))
    if good:
        oid = "demo-org-d"
        record(
            db,
            f"membership:{good.id}:{oid}",
            "membership",
            {
                "user_id": good.id,
                "roles": ["CLIENT_SIGNER", "CLIENT_PREPARER"],
                "valid_from": "2026-01-01",
                "valid_to": "2029-10-25",
                "actions": ["prepare", "submit", "consent", "accept", "decline", "withdraw", "sign", "draw", "repay"],
            },
            oid,
        )
    for email, name, role in [
        ("prepare", "Бухгалтер · подготовка", "entrepreneur"),
        ("bank2", "Второй кредитный аналитик", "bank"),
        ("admin", "Оператор демонстрации", "demo_admin"),
    ]:
        user = db.scalar(select(User).where(User.email == f"{email}@demo.local"))
        if not user:
            user = User(
                id=str(uuid4()),
                email=f"{email}@demo.local",
                name=name,
                role=role,
                company_id=None,
                password_hash=password_hash("Demo2026!"),
            )
            db.add(user)
        if email == "prepare":
            record(
                db,
                f"membership:{user.id}:demo-org-b",
                "membership",
                {
                    "user_id": user.id,
                    "roles": ["CLIENT_PREPARER"],
                    "valid_from": "2026-01-01",
                    "valid_to": "2029-10-25",
                    "actions": ["prepare"],
                },
                "demo-org-b",
            )
    db.flush()
