"""Deterministic teaching model. No real banking terms, external data or AI."""

from decimal import Decimal, ROUND_HALF_UP, localcontext
from fastapi import HTTPException
from sqlalchemy import select
from .models import Audit, Document, Notification, User, now

RULE_VERSION = "demo-v1"
PRODUCTS = {
    "working": {
        "id": "working",
        "name": "Оборотный кредит",
        "description": "Запасы, закупки и текущие расходы бизнеса. Демонстрационные условия.",
        "min_amount": "100000.00",
        "max_amount": "5000000.00",
        "min_months": 3,
        "max_months": 36,
        "annual_rate": "18.00",
        "purpose": "Пополнение оборотных средств",
    },
    "equipment": {
        "id": "equipment",
        "name": "Кредит на оборудование",
        "description": "Техника и оборудование для развития. Демонстрационные условия.",
        "min_amount": "250000.00",
        "max_amount": "10000000.00",
        "min_months": 6,
        "max_months": 60,
        "annual_rate": "15.50",
        "purpose": "Приобретение оборудования",
    },
}
ATTENTION_STATUSES = {"manual_review", "check_failed"}
BANK_ACTIONS = {"approve", "reject", "offer", "request_info"}
ENTREPRENEUR_TRANSITIONS = {
    "submit": {"draft"},
    "retry": {"check_failed"},
    "respond": {"needs_info"},
    "accept": {"approved", "alternative_offered"},
    "decline": {"approved", "alternative_offered"},
    "sign": {"accepted"},
    "disburse": {"signed"},
    "cancel": {"draft", "needs_info", "check_failed", "manual_review", "approved", "alternative_offered"},
}
ALLOWED_TRANSITIONS = {
    "draft": {"submitted", "cancelled"},
    "submitted": {"checking"},
    "checking": {"needs_info", "scoring", "check_failed", "rejected"},
    "needs_info": {"checking", "cancelled"},
    "scoring": {"approved", "rejected", "alternative_offered", "manual_review"},
    "manual_review": {"needs_info", "approved", "rejected", "alternative_offered", "cancelled"},
    "approved": {"accepted", "declined", "cancelled"},
    "alternative_offered": {"accepted", "declined", "cancelled"},
    "accepted": {"signed"},
    "signed": {"disbursed"},
    "check_failed": {"checking", "cancelled"},
    "rejected": set(),
    "disbursed": set(),
    "declined": set(),
    "cancelled": set(),
}
STATUS_LABELS = {
    "draft": "Черновик",
    "submitted": "Заявка отправлена",
    "checking": "Проверяем сведения",
    "needs_info": "Нужны сведения",
    "scoring": "Оцениваем заявку",
    "manual_review": "Рассмотрение сотрудником",
    "approved": "Одобрено",
    "rejected": "Отказ",
    "alternative_offered": "Предложены другие условия",
    "accepted": "Условия приняты",
    "signed": "Демо-документы подписаны",
    "disbursed": "Демо-выдача выполнена",
    "declined": "Предложение отклонено",
    "cancelled": "Заявка отменена",
    "check_failed": "Проверка временно недоступна",
}


def money(value):
    return str(Decimal(str(value)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP))


def validate_terms(product_id, amount, months):
    product = PRODUCTS.get(product_id)
    if not product:
        raise HTTPException(422, "Неизвестный продукт")
    if not Decimal(product["min_amount"]) <= Decimal(amount) <= Decimal(product["max_amount"]):
        raise HTTPException(422, f"Сумма для продукта: от {product['min_amount']} до {product['max_amount']} ₽")
    if not product["min_months"] <= months <= product["max_months"]:
        raise HTTPException(422, f"Срок для продукта: от {product['min_months']} до {product['max_months']} месяцев")
    return product


def calculate(product_id, amount, months):
    product = validate_terms(product_id, amount, months)
    amount = Decimal(amount)
    with localcontext() as ctx:
        ctx.prec = 40
        monthly_rate = Decimal(product["annual_rate"]) / 1200
        factor = (1 + monthly_rate) ** months
        payment = (amount * monthly_rate * factor / (factor - 1)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        total = payment * months
    return {"payment": money(payment), "total": money(total), "overpayment": money(total - amount), "annual_rate": product["annual_rate"]}


def snapshot(company):
    return {key: getattr(company, key) for key in ("id", "name", "inn", "registration_months", "industry")} | {
        key: money(getattr(company, key)) for key in ("revenue", "expenses", "monthly_debt")
    }


def transition(db, application, status, actor, reason):
    initial_draft = application.status == status == "draft" and not application.history
    if not initial_draft and status not in ALLOWED_TRANSITIONS.get(application.status, set()):
        raise HTTPException(409, "Недопустимый переход состояния заявки")
    if not actor or not reason:
        raise HTTPException(422, "Для перехода необходимы инициатор и основание")
    stamp = now()
    application.status = status
    application.updated_at = stamp
    application.history = list(application.history or []) + [
        {
            "status": status,
            "actor": actor,
            "reason": reason,
            "created_at": stamp.isoformat(),
            "financial_snapshot": {
                key: money(getattr(application, key)) if getattr(application, key) is not None else None
                for key in ("amount", "revenue", "expenses", "monthly_debt")
            }
            | {"months": application.months, "product_id": application.product_id},
        }
    ]
    db.add(Audit(application_id=application.id, actor=actor, action=status, reason=reason))
    if status not in {"draft", "submitted", "checking", "scoring"}:
        recipients = db.scalars(select(User).where(User.company_id == application.company_id)).all()
        for user in recipients:
            db.add(
                Notification(
                    user_id=user.id,
                    application_id=application.id,
                    message=f"{application.number}: {STATUS_LABELS[status]}. {reason}"[:1000],
                )
            )
        if status in ATTENTION_STATUSES:
            for user in db.scalars(select(User).where(User.role == "bank")).all():
                db.add(
                    Notification(user_id=user.id, application_id=application.id, message=f"{application.number}: {STATUS_LABELS[status]}")
                )


def score(application, fixture):
    factors = []

    def add(name, points, detail):
        factors.append({"name": name, "points": points, "detail": detail})

    age = application.company_snapshot["registration_months"]
    add("Срок работы", 40 if age < 6 else 25 if age < 12 else 10 if age < 24 else 0, f"{age} месяцев")
    add(
        "Кредитная история",
        80 if fixture.get("overdue") else 0,
        "Есть непогашенные просрочки" if fixture.get("overdue") else "Просрочек не обнаружено",
    )
    revenue = Decimal(application.revenue)
    expenses = Decimal(application.expenses)
    debt = Decimal(application.monthly_debt)
    free = revenue - expenses
    add(
        "Денежный поток",
        50 if free <= 0 else 0,
        "Доходы не покрывают расходы" if free <= 0 else f"До обслуживания долга: {money(free)} ₽ / мес.",
    )
    add("Текущие обязательства", 25 if revenue <= 0 or debt > revenue * Decimal(".3") else 0, f"{money(debt)} ₽ / мес.")
    pressure = (Decimal(application.calculation["payment"]) + debt) / max(free, Decimal("1"))
    add("Нагрузка с новым кредитом", 20 if pressure > Decimal(".6") else 0, f"{money(pressure * 100)}% свободного потока")
    value = min(sum(f["points"] for f in factors), 100)
    return {
        "score": value,
        "level": "low" if value <= 25 else "medium" if value < 50 else "high",
        "factors": factors,
        "version": RULE_VERSION,
    }


def decide(application, fixture, risk):
    """Product policy separated from source checks and credit risk scoring."""
    age = application.company_snapshot["registration_months"]
    if fixture.get("overdue"):
        return "reject", "По результатам проверки кредитной истории финансирование сейчас недоступно.", None
    if risk["score"] >= 50:
        return "reject", "Текущие финансовые показатели не позволяют безопасно обслуживать кредит.", None
    minimum_age = 12 if application.product_id == "working" else 18
    if age < minimum_age or risk["score"] > (25 if application.product_id == "working" else 20):
        return "manual_review", "Заявке требуется оценка сотрудника с учётом срока работы и условий продукта.", None
    available = max(
        Decimal("0"), (Decimal(application.revenue) - Decimal(application.expenses)) * Decimal(".6") - Decimal(application.monthly_debt)
    )
    payment = Decimal(application.calculation["payment"])
    if payment > available:
        max_amount = (Decimal(application.amount) * available / payment).quantize(Decimal("1000"), rounding="ROUND_DOWN")
        # ROUND_DOWN to a whole thousand rather than just matching decimal exponent.
        max_amount = (max_amount // 1000) * 1000
        if max_amount < Decimal(PRODUCTS[application.product_id]["min_amount"]):
            return "reject", "Свободного денежного потока недостаточно для минимальной суммы продукта.", None
        terms = {"amount": money(max_amount), "months": application.months} | calculate(
            application.product_id, max_amount, application.months
        )
        return "offer", "Предлагаем меньшую сумму, чтобы платёж соответствовал денежному потоку.", terms
    return "approve", "Проверки пройдены. Платёж соответствует заявленным финансовым показателям.", None


def process(db, application, company):
    application.processing_attempts += 1
    transition(db, application, "checking", "Система", "Запущены локальные синтетические проверки")
    fixture = company.fixture
    checks = [
        {
            "name": "Регистрация компании",
            "status": "passed" if fixture.get("active", True) else "failed",
            "detail": "Компания действует в демонстрационном реестре" if fixture.get("active", True) else "Компания не действует",
            "source": "Локальный симулятор реестра",
            "version": RULE_VERSION,
        }
    ]
    if fixture.get("fail_once") and application.processing_attempts == 1:
        checks.append(
            {
                "name": "Кредитная история",
                "status": "failed",
                "detail": "Воспроизводимый временный сбой симулятора. Повторите проверку.",
                "source": "Локальный симулятор БКИ",
                "version": RULE_VERSION,
            }
        )
        application.checks = checks
        transition(db, application, "check_failed", "Система", "Источник проверки временно недоступен. Повторите обработку.")
        return
    checks.append(
        {
            "name": "Кредитная история",
            "status": "failed" if fixture.get("overdue") else "passed",
            "detail": "Непогашенная просрочка в синтетическом БКИ" if fixture.get("overdue") else "Просрочек нет",
            "source": "Локальный симулятор БКИ",
            "version": RULE_VERSION,
        }
    )
    missing = [name for name in ("revenue", "expenses", "monthly_debt") if getattr(application, name) is None]
    documents = db.scalars(select(Document.id).where(Document.application_id == application.id)).first()
    if documents is None:
        missing.append("documents")
    checks.append(
        {
            "name": "Полнота сведений",
            "status": "warning" if missing else "passed",
            "detail": "Не хватает финансовых сведений или документов" if missing else "Обязательные сведения заполнены",
            "source": "Проверка данных заявки",
            "version": RULE_VERSION,
        }
    )
    application.checks = checks
    if missing:
        application.info_request = "Укажите месячную выручку, расходы и обязательства; приложите документ о финансовых показателях."
        application.recommendation = {"type": "needs_info", "reason": application.info_request, "version": RULE_VERSION}
        transition(db, application, "needs_info", "Система", application.info_request)
        return
    if not fixture.get("active", True):
        application.recommendation = {
            "type": "reject",
            "reason": "Компания не действует в демонстрационном реестре.",
            "version": RULE_VERSION,
        }
        application.decision = {"type": "reject", "reason": application.recommendation["reason"], "source": "automatic"}
        transition(db, application, "rejected", "Система", application.decision["reason"])
        return
    application.info_request = None
    transition(db, application, "scoring", "Система", "Проверки завершены. Рассчитывается оценка по учебным правилам.")
    application.risk = score(application, fixture)
    kind, reason, offer = decide(application, fixture, application.risk)
    application.recommendation = {"type": kind, "reason": reason, "version": RULE_VERSION}
    application.evaluation_history = list(application.evaluation_history or []) + [
        {
            "created_at": now().isoformat(),
            "version": RULE_VERSION,
            "company_snapshot": application.company_snapshot,
            "financial_snapshot": {key: money(getattr(application, key)) for key in ("amount", "revenue", "expenses", "monthly_debt")}
            | {"months": application.months, "product_id": application.product_id},
            "checks": checks,
            "risk": application.risk,
            "recommendation": application.recommendation,
        }
    ]
    application.offer = offer
    application.decision = {"type": kind, "reason": reason, "source": "automatic"} if kind != "manual_review" else None
    transition(
        db,
        application,
        {"approve": "approved", "reject": "rejected", "offer": "alternative_offered", "manual_review": "manual_review"}[kind],
        "Система",
        reason,
    )
