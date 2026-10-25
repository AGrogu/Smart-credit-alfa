"""Idempotent synthetic demo fixtures. Never mutates existing demo users/apps."""

from decimal import Decimal
from sqlalchemy import select
from .models import Application, Company, Document, User
from .security import password_hash
from .lending import calculate, snapshot, transition

SCENARIOS = [
    ("good", "ООО «Пекарня Утро»", "7720000101", 48, {}, "500000.00"),
    ("risk", "ООО «Северный маршрут»", "7720000102", 36, {"overdue": True}, "500000.00"),
    ("info", "ООО «Мастерская Формат»", "7720000103", 30, {}, "500000.00"),
    ("review", "ООО «Студия Рост»", "7720000104", 8, {}, "500000.00"),
    ("offer", "ООО «Точка запаса»", "7720000105", 42, {}, "4500000.00"),
    ("retry", "ООО «ТехноСервис»", "7720000106", 60, {"fail_once": True}, "500000.00"),
]


def seed(db):
    if not db.scalar(select(User).where(User.email == "bank@demo.local")):
        db.add(User(email="bank@demo.local", name="Мария — кредитный эксперт", role="bank", password_hash=password_hash("Demo2026!")))
        db.flush()
    for code, name, inn, age, fixture, amount in SCENARIOS:
        if db.scalar(select(User).where(User.email == f"{code}@demo.local")):
            continue
        company = Company(
            name=name,
            inn=inn,
            registration_months=age,
            industry="Малый бизнес",
            revenue=Decimal("1000000"),
            expenses=Decimal("600000"),
            monthly_debt=Decimal("20000"),
            fixture={"active": True, **fixture},
        )
        db.add(company)
        db.flush()
        user = User(
            email=f"{code}@demo.local",
            name=f"Предприниматель · {name}",
            role="entrepreneur",
            company_id=company.id,
            password_hash=password_hash("Demo2026!"),
        )
        db.add(user)
        application = Application(
            number=f"ДЕМО-{code.upper()}",
            company_id=company.id,
            product_id="working",
            amount=Decimal(amount),
            months=12,
            purpose="Пополнение запасов",
            revenue=None if code == "info" else company.revenue,
            expenses=company.expenses,
            monthly_debt=company.monthly_debt,
            company_snapshot=snapshot(company),
            calculation=calculate("working", Decimal(amount), 12),
        )
        db.add(application)
        db.flush()
        transition(db, application, "draft", "Система", "Синтетический пример. Проверьте сведения и отправьте заявку.")
        content = f"ДЕМОНСТРАЦИОННЫЙ ДОКУМЕНТ\n{name}\nДанные синтетические. Не является бухгалтерской отчётностью.\nВыручка: 1 000 000 рублей/мес. Расходы: 600 000 рублей/мес.\n".encode()
        db.add(
            Document(
                application_id=application.id,
                name=f"demo-financials-{code}.txt",
                media_type="text/plain; charset=utf-8",
                size=len(content),
                content=content,
            )
        )
    db.flush()


if __name__ == "__main__":
    from .db import SessionLocal

    with SessionLocal() as db:
        seed(db)
        db.commit()
        print("Синтетические демо-данные готовы; существующие записи сохранены.")
