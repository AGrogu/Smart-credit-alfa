"""Explainable, deterministic synthetic lending policy from the supplied TZ.

This is DEMO_POLICY_1, never Alfa Bank's tariff, PD model or risk policy.
Inputs/outputs are JSON-native snapshots; callers persist their exact versions.
"""

from datetime import timedelta
from decimal import Decimal, localcontext

from .credit_servicing import (
    CreditDomainError,
    act365_interest,
    add_months,
    annuity_schedule,
    calendar_date,
    money,
    rounded,
)

POLICY_VERSION = "DEMO_POLICY_1"
FLOW_FIELDS = (
    "customer_receipts_kopecks",
    "operating_payments_kopecks",
    "taxes_kopecks",
    "maintenance_capex_kopecks",
    "mandatory_withdrawals_kopecks",
)
PRODUCT_VERSIONS = {
    "DEMO_TERM_LOAN_V1": {
        "code": "DEMO_TERM_LOAN_V1",
        "name": "Разовый кредит",
        "mechanism": "TERM_LOAN",
        "minimum_kopecks": 30_000_000,
        "maximum_kopecks": 500_000_000,
        "terms_months": [6, 12, 18, 24],
        "annual_rate_bps": 2400,
        "minimum_age_months": 9,
        "fees_kopecks": 0,
        "collateral": False,
        "guarantor": False,
        "currency": "RUB",
    },
    "DEMO_REVOLVING_V1": {
        "code": "DEMO_REVOLVING_V1",
        "name": "Возобновляемая кредитная линия",
        "mechanism": "REVOLVING",
        "minimum_kopecks": 30_000_000,
        "maximum_kopecks": 300_000_000,
        "terms_months": [12],
        "tranche_days": [30, 60, 90],
        "minimum_tranche_kopecks": 5_000_000,
        "maximum_open_tranches": 3,
        "annual_rate_bps": 2400,
        "minimum_age_months": 9,
        "fees_kopecks": 0,
        "collateral": False,
        "guarantor": False,
        "currency": "RUB",
    },
}
DEMO_NOTICE = "Демонстрационные условия. Реальные операции не выполняются"


def cfads(period: dict) -> int:
    values = {key: money(period[key], key) for key in FLOW_FIELDS}
    return values["customer_receipts_kopecks"] - sum(values[key] for key in FLOW_FIELDS[1:])


def ratio(numerator, denominator) -> str | None:
    return str((Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.0001"))) if denominator else None


def full_months(registration_date: str, on_date: str) -> int:
    start, end = calendar_date(registration_date), calendar_date(on_date)
    if start > end:
        raise CreditDomainError("REGISTRATION_IN_FUTURE", "Дата регистрации позже даты подачи")
    return (end.year - start.year) * 12 + end.month - start.month - (end.day < start.day)


def evaluate_term(
    amount_kopecks: int,
    months: int,
    forecast: list[dict],
    initial_cash_kopecks: int,
    disbursement_date: str = "2026-10-25",
    annual_rate_bps: int = 2400,
) -> dict:
    """Evaluate every payment interval, never replace the calendar by an average."""
    initial = money(initial_cash_kopecks, "initial_cash_kopecks")
    schedule = annuity_schedule(amount_kopecks, months, disbursement_date, annual_rate_bps)
    if len(forecast) < months:
        return {
            "passed": False,
            "code": "DATA_MISSING",
            "missing_forecast_periods": list(range(len(forecast) + 1, months + 1)),
            "schedule": schedule,
            "periods": [],
        }
    cash = initial
    stress_cash = Decimal(initial)
    cumulative = minimum_cumulative = 0
    periods = []
    with localcontext() as ctx:
        ctx.prec = 50
        for index, row in enumerate(schedule["rows"]):
            flow = cfads(forecast[index])
            old_debt = money(forecast[index]["old_debt_service_kopecks"], "old_debt_service_kopecks")
            debt = old_debt + row["payment_kopecks"]
            stress = Decimal(flow) * Decimal("0.90")
            cash += flow - debt
            stress_cash += stress - debt
            cumulative += flow - debt
            minimum_cumulative = min(minimum_cumulative, cumulative)
            base_ok = Decimal(flow) >= Decimal(debt) * Decimal("1.30")
            stress_ok = stress >= Decimal(debt) * Decimal("1.10")
            periods.append(
                {
                    "number": index + 1,
                    "from_date": disbursement_date if index == 0 else schedule["rows"][index - 1]["due_on"],
                    "to_date": row["due_on"],
                    "cfads_kopecks": flow,
                    "stress_cfads_kopecks": rounded(stress),
                    "old_debt_service_kopecks": old_debt,
                    "new_debt_service_kopecks": row["payment_kopecks"],
                    "debt_service_kopecks": debt,
                    "dscr": ratio(flow, debt),
                    "stress_dscr": ratio(stress, debt),
                    "ending_cash_kopecks": cash,
                    "stress_ending_cash_kopecks": rounded(stress_cash),
                    "base_passed": base_ok,
                    "stress_passed": stress_ok,
                    "liquidity_passed": cash >= 0,
                    "passed": base_ok and stress_ok and cash >= 0,
                }
            )
    total_cfads = sum(item["cfads_kopecks"] for item in periods)
    total_debt = sum(item["debt_service_kopecks"] for item in periods)
    passed = all(item["passed"] for item in periods)
    return {
        "passed": passed,
        "code": "CASHFLOW_SUFFICIENT" if passed else "CASHFLOW_INSUFFICIENT",
        "amount_kopecks": amount_kopecks,
        "months": months,
        "annual_rate_bps": annual_rate_bps,
        "schedule": schedule,
        "periods": periods,
        "initial_cash_kopecks": initial,
        "minimum_cash_kopecks": min(item["ending_cash_kopecks"] for item in periods),
        "required_initial_cash_kopecks": -minimum_cumulative,
        "average_dscr": ratio(total_cfads, total_debt),
        "minimum_dscr": min(periods, key=lambda item: Decimal(item["dscr"]))["dscr"],
        "minimum_stress_dscr": min(periods, key=lambda item: Decimal(item["stress_dscr"]))["stress_dscr"],
        "failed_period_numbers": [item["number"] for item in periods if not item["passed"]],
        "base_threshold": "1.30",
        "stress_threshold": "1.10",
        "stress_cfads_multiplier": "0.90",
        "intramonth_liquidity_modelled": False,
    }


def evaluate_cycle(plan: dict, amount_kopecks: int, days: int, disbursement_date: str, annual_rate_bps: int = 2400) -> dict:
    """Dated full-body repayment test; sales after maturity cannot finance return.

    Event IDs must be unique. Optional existing_repayments keep already issued
    debt in the common calendar: [{id,date,total_kopecks}]. They are included
    in the coverage denominator as well as the dated cash balance.
    """
    amount = money(amount_kopecks, positive=True)
    if isinstance(days, bool) or days not in {30, 60, 90}:
        raise CreditDomainError("INVALID_TRANCHE_TERM", "Нужен цикл 30, 60 или 90 дней")
    start = calendar_date(disbursement_date)
    end = start + timedelta(days=days)
    initial = money(plan["initial_cash_kopecks"], "initial_cash_kopecks")
    events = plan.get("events", [])
    identifiers = set()
    ordering = {
        "DRAW": 0,
        "TARGET_PAYMENT": 1,
        "OPERATING_PAYMENT": 2,
        "TAX_PAYMENT": 2,
        "OTHER_PAYMENT": 2,
        "CUSTOMER_RECEIPT": 3,
        "OLD_DEBT_PAYMENT": 4,
        "REPAYMENT": 5,
    }
    calendar = [{"id": "new-draw", "date": start.isoformat(), "kind": "DRAW", "amount_kopecks": amount}]
    net = target = 0
    late_receipts = []
    invalid = []
    for event in events:
        identifier = event.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise CreditDomainError("DUPLICATE_CASHFLOW", "Денежное событие должно иметь уникальный идентификатор")
        identifiers.add(identifier)
        when = calendar_date(event["date"])
        kind = event["kind"]
        if kind not in ordering or kind in {"DRAW", "REPAYMENT"}:
            raise CreditDomainError("INVALID_CYCLE_EVENT", "Выдачу и новый возврат рассчитывает сервер")
        value = money(event["amount_kopecks"])
        if when < start:
            invalid.append(identifier)
            continue
        if when > end:
            if kind == "CUSTOMER_RECEIPT":
                late_receipts.append(identifier)
            continue
        if kind == "CUSTOMER_RECEIPT":
            net += value
        elif kind == "TARGET_PAYMENT":
            target += value
        else:
            net -= value
        calendar.append({"id": identifier, "date": when.isoformat(), "kind": kind, "amount_kopecks": value})
    interest = act365_interest(amount, days, annual_rate_bps)["interest_kopecks"]
    repayment = amount + interest
    total_return = repayment
    for event in plan.get("existing_repayments", []):
        identifier = event.get("id")
        if not identifier or identifier in identifiers:
            raise CreditDomainError("DUPLICATE_CASHFLOW", "Существующее обязательство должно учитываться ровно один раз")
        identifiers.add(identifier)
        when = calendar_date(event["date"])
        value = money(event["total_kopecks"], positive=True)
        if start <= when <= end:
            total_return += value
            calendar.append({"id": identifier, "date": when.isoformat(), "kind": "REPAYMENT", "amount_kopecks": value})
    calendar.append({"id": "new-repayment", "date": end.isoformat(), "kind": "REPAYMENT", "amount_kopecks": repayment})
    calendar.sort(key=lambda event: (event["date"], ordering[event["kind"]], event["id"]))
    cash = initial
    minimum = cash
    for event in calendar:
        cash += event["amount_kopecks"] if event["kind"] in {"DRAW", "CUSTOMER_RECEIPT"} else -event["amount_kopecks"]
        event["ending_cash_kopecks"] = cash
        minimum = min(minimum, cash)
    with localcontext() as ctx:
        ctx.prec = 50
        base_ok = Decimal(net) >= Decimal(total_return) * Decimal("1.10")
        stress_ok = Decimal(net) * Decimal("0.90") >= total_return
    target_ok = target == amount
    passed = base_ok and stress_ok and minimum >= 0 and target_ok and not invalid and not late_receipts
    reasons = []
    if not target_ok:
        reasons.append("TARGET_BUDGET_MISMATCH")
    if invalid:
        reasons.append("PLAN_BEFORE_DISBURSEMENT")
    if late_receipts:
        reasons.append("MATURITY_MISMATCH")
    if not base_ok or not stress_ok or minimum < 0:
        reasons.append("CASHFLOW_INSUFFICIENT")
    return {
        "passed": passed,
        "amount_kopecks": amount,
        "days": days,
        "issued_on": start.isoformat(),
        "matures_on": end.isoformat(),
        "interest_kopecks": interest,
        "repayment_kopecks": repayment,
        "net_cycle_funds_kopecks": net,
        "total_return_kopecks": total_return,
        "coverage": ratio(net, total_return),
        "stress_coverage": ratio(Decimal(net) * Decimal("0.90"), total_return),
        "base_passed": base_ok,
        "stress_passed": stress_ok,
        "liquidity_passed": minimum >= 0,
        "target_budget_passed": target_ok,
        "minimum_cash_kopecks": minimum,
        "ending_cash_kopecks": cash,
        "calendar": calendar,
        "late_receipt_ids": late_receipts,
        "reasons": reasons,
        "base_threshold": "1.10",
        "stress_threshold": "1.00",
        "metric": "CYCLE_COVERAGE_NOT_REGULATORY_DSCR",
    }


def _reason(code, message, **details):
    return {"code": code, "message": message, **details}


def _risk(payload, age):
    historical = [cfads(item) for item in payload["history"]]
    forecast = [cfads(item) for item in payload["forecast"][:3]]
    receipts = [Decimal(item["customer_receipts_kopecks"]) for item in payload["history"]]
    with localcontext() as ctx:
        ctx.prec = 50
        mean = sum(receipts) / 6
        cv = (sum((value - mean) ** 2 for value in receipts) / 6).sqrt() / mean if mean else None
    c = []
    b = []
    if payload["active_overdue_days"] >= 30:
        c.append(_reason("RISK_ACTIVE_OVERDUE_30", "Подтверждена активная просрочка от 30 дней"))
    if sum(value < 0 for value in historical) >= 4 and len(forecast) >= 3 and all(value < 0 for value in forecast):
        c.append(
            _reason("RISK_PERSISTENT_NEGATIVE_CFADS", "Устойчивый отрицательный денежный поток и отрицательный подтвержденный прогноз")
        )
    if age < 12:
        b.append(_reason("RISK_SHORT_HISTORY", "Стаж 9–11 полных месяцев"))
    if 1 <= payload["active_overdue_days"] <= 29:
        b.append(_reason("RISK_ACTIVE_OVERDUE", "Активная просрочка 1–29 дней"))
    if payload["closed_overdue_last12"]:
        b.append(_reason("RISK_CLOSED_OVERDUE", "Закрытая просрочка за последние 12 месяцев"))
    if payload["concentration_bps"] > 5000:
        b.append(_reason("RISK_CONCENTRATION", "Доля крупнейшего покупателя выше 50%"))
    if cv is not None and cv > Decimal("0.35"):
        b.append(_reason("RISK_VOLATILE_RECEIPTS", "Коэффициент вариации поступлений выше 0,35"))
    if mean == 0:
        b.append(_reason("RISK_ZERO_RECEIPTS", "Средние покупательские поступления равны нулю"))
    return {
        "category": "C" if c else "B" if b else "A",
        "reasons": c + b,
        "metrics": {
            "age_months": age,
            "historical_cfads_kopecks": historical,
            "mean_customer_receipts_kopecks": str(mean),
            "receipts_population_cv": str(cv) if cv is not None else None,
            "negative_history_months": sum(value < 0 for value in historical),
            "concentration_bps": payload["concentration_bps"],
        },
        "is_probability": False,
        "policy_version": POLICY_VERSION,
    }


def _missing_inputs(payload, product):
    problems = []
    required = [
        "initial_cash_kopecks",
        "concentration_bps",
        "active_overdue_days",
        "closed_overdue_last12",
        "seasonal",
        "financials_confirmed",
        "documents_complete",
        "documents_verified",
    ]
    for field in required:
        if payload.get(field) is None:
            problems.append(_reason("DATA_MISSING", "Нужно заполнить обязательное поле", field=field))
    for field in ("closed_overdue_last12", "seasonal", "financials_confirmed", "documents_complete", "documents_verified"):
        if payload.get(field) is not None and type(payload[field]) is not bool:
            problems.append(_reason("DATA_CONTRADICTORY", "Нужно явное логическое подтверждение", field=field))
    history = payload.get("history", [])
    expected = [add_months(payload["submitted_on"][:7] + "-01", index)[:7] for index in range(-6, 0)]
    actual = [item.get("period") for item in history]
    if actual != expected:
        problems.append(
            _reason(
                "DATA_MISSING",
                "Нужны шесть последовательных завершенных месяцев перед подачей",
                field="history",
                expected_periods=expected,
                received_periods=actual,
            )
        )
    forecast = payload.get("forecast", [])
    if any("period" in item for item in forecast):
        expected_forecast = [add_months(payload["submitted_on"], index + 1)[:7] for index in range(len(forecast))]
        if [item.get("period") for item in forecast] != expected_forecast:
            problems.append(
                _reason(
                    "DATA_CONTRADICTORY",
                    "Прогноз должен покрывать последовательные платежные периоды без пропусков и повторов",
                    field="forecast",
                    expected_periods=expected_forecast,
                )
            )
    for block in ("history", "forecast"):
        for index, item in enumerate(payload.get(block, [])):
            for field in FLOW_FIELDS + (("old_debt_service_kopecks",) if block == "forecast" else ()):
                if item.get(field) is None:
                    problems.append(_reason("DATA_MISSING", "Нужно подтвердить денежный поток за период", field=f"{block}.{index}.{field}"))
                else:
                    try:
                        money(item[field], field)
                    except CreditDomainError:
                        problems.append(
                            _reason(
                                "DATA_CONTRADICTORY",
                                "Исходные денежные потоки должны быть неотрицательными целыми копейками",
                                field=f"{block}.{index}.{field}",
                            )
                        )
    minimum_forecast = payload["term_months"] if product["mechanism"] == "TERM_LOAN" else 3
    if len(payload.get("forecast", [])) < minimum_forecast:
        problems.append(
            _reason("DATA_MISSING", "Не хватает подтвержденных прогнозных периодов", field="forecast", minimum_periods=minimum_forecast)
        )
    if payload.get("financials_confirmed") is False:
        problems.append(_reason("DATA_MISSING", "Подписант должен подтвердить прогноз и его допущения", field="financials_confirmed"))
    if payload.get("documents_complete") is False:
        problems.append(_reason("DATA_MISSING", "Не хватает применимых документов", field="documents_complete"))
    need = payload.get("need", {})
    for field in ("minimum_useful_kopecks", "partial_feasible", "total_cost_kopecks", "own_contribution_kopecks", "purpose"):
        if need.get(field) is None:
            problems.append(_reason("DATA_MISSING", "Нужно уточнить бюджет и реализуемость потребности", field=f"need.{field}"))
    if need.get("partial_feasible") is not None and type(need["partial_feasible"]) is not bool:
        problems.append(_reason("DATA_CONTRADICTORY", "Нужно подтвердить возможность частичной реализации", field="need.partial_feasible"))
    if product["mechanism"] == "REVOLVING":
        if not payload.get("cycle_plan", {}).get("confirmed"):
            problems.append(_reason("DATA_MISSING", "Нужно подтвердить календарь полного лимита на 90 дней", field="cycle_plan"))
    return problems


def evaluate_application(payload: dict) -> dict:
    """Run applicability → data → sources → reconciliation → risk → structure.

    Mandatory authorization/consent gates are performed by the API before this
    pure evaluator. No negative economic decision is inferred automatically.
    """
    result = {
        "policy_version": POLICY_VERSION,
        "product_version": payload.get("product_code"),
        "notice": DEMO_NOTICE,
        "status": None,
        "decision": None,
        "risk": {"category": "NOT_CALCULATED", "reasons": []},
        "reasons": [],
        "original": None,
        "alternatives": [],
        "offer": None,
        "manual_approval_allowed": False,
    }

    def stop(status, reasons):
        result.update(status=status, reasons=reasons)
        return result

    for field in ("organization", "need", "source_checks"):
        if not isinstance(payload.get(field, {}), dict):
            return stop("NEEDS_INFO", [_reason("DATA_CONTRADICTORY", "Неверный формат блока данных", field=field)])
    for field in ("history", "forecast"):
        values = payload.get(field, [])
        if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
            return stop("NEEDS_INFO", [_reason("DATA_CONTRADICTORY", "Нужна таблица финансовых периодов", field=field)])
        if len(values) > (6 if field == "history" else 24):
            return stop("NEEDS_INFO", [_reason("DATA_CONTRADICTORY", "Слишком много финансовых периодов", field=field)])
    if not isinstance(payload.get("cycle_plan", {}), dict):
        return stop("NEEDS_INFO", [_reason("DATA_CONTRADICTORY", "Неверный формат календаря цикла", field="cycle_plan")])

    product = PRODUCT_VERSIONS.get(payload.get("product_code"))
    if not product:
        return stop("NOT_ELIGIBLE", [_reason("PRODUCT_UNKNOWN", "Выбранный продукт не реализован")])
    try:
        amount = money(payload["amount_kopecks"], positive=True)
        months = payload["term_months"]
        submitted = calendar_date(payload["submitted_on"]).isoformat()
        organization = payload["organization"]
        age = full_months(organization["registration_date"], submitted)
    except (KeyError, CreditDomainError, TypeError) as exc:
        return stop("NEEDS_INFO", [_reason("DATA_MISSING", "Нужно уточнить заемщика, сумму, срок и дату", detail=str(exc))])
    formal = []
    if organization.get("legal_form") not in {"IP", "OOO"}:
        formal.append(_reason("PRODUCT_LEGAL_FORM", "Демопродукт доступен только ИП и ООО"))
    if organization.get("status") in {"LIQUIDATED", "INACTIVE"}:
        formal.append(_reason("PRODUCT_STATUS", "Подтвержден недействующий статус заемщика"))
    elif organization.get("status") != "ACTIVE":
        return stop("MANUAL_REVIEW", [_reason("PRODUCT_STATUS_UNKNOWN", "Статус заемщика требует ручной проверки")])
    if age < product["minimum_age_months"]:
        formal.append(_reason("PRODUCT_AGE", "Для выбранного демопродукта нужно не менее 9 полных месяцев работы", age_months=age))
    if not product["minimum_kopecks"] <= amount <= product["maximum_kopecks"]:
        formal.append(_reason("PRODUCT_AMOUNT", "Запрос выходит за границы выбранного демопродукта"))
    if type(months) is not int or months not in product["terms_months"]:
        formal.append(_reason("PRODUCT_TERM", "Срок не предусмотрен выбранным демопродуктом"))
    if formal:
        return stop("NOT_ELIGIBLE", formal)
    missing = _missing_inputs(payload, product)
    if missing:
        return stop("NEEDS_INFO", missing)
    for field in ("initial_cash_kopecks", "active_overdue_days", "concentration_bps"):
        try:
            money(payload[field], field)
        except CreditDomainError:
            return stop("NEEDS_INFO", [_reason("DATA_CONTRADICTORY", "Некорректное значение", field=field)])
    if payload["concentration_bps"] > 10000:
        return stop("NEEDS_INFO", [_reason("DATA_CONTRADICTORY", "Доля покупателя не может превышать 100%", field="concentration_bps")])
    need = payload["need"]
    try:
        for field in ("minimum_useful_kopecks", "total_cost_kopecks", "own_contribution_kopecks"):
            money(need[field], field)
    except CreditDomainError:
        return stop("NEEDS_INFO", [_reason("DATA_CONTRADICTORY", "Некорректный бюджет потребности")])
    if need["minimum_useful_kopecks"] > amount:
        return stop("NEEDS_INFO", [_reason("DATA_CONTRADICTORY", "Минимально полезная сумма выше запроса")])
    if not need["partial_feasible"] and need["minimum_useful_kopecks"] != amount:
        return stop(
            "NEEDS_INFO", [_reason("DATA_CONTRADICTORY", "Для неделимой задачи минимально полезная сумма должна равняться запросу")]
        )
    if "available_own_funds_kopecks" in payload:
        try:
            available_own = money(payload["available_own_funds_kopecks"], "available_own_funds_kopecks")
            committed_own = money(payload.get("already_committed_own_funds_kopecks", 0), "already_committed_own_funds_kopecks")
        except CreditDomainError:
            return stop("NEEDS_INFO", [_reason("DATA_CONTRADICTORY", "Нужно подтвердить остаток собственных средств")])
        if payload["initial_cash_kopecks"] + need["own_contribution_kopecks"] + committed_own > available_own:
            return stop(
                "NEEDS_INFO",
                [
                    _reason(
                        "DATA_CONTRADICTORY",
                        "Собственные средства обещаны в нескольких бюджетах одновременно",
                        field="initial_cash_kopecks",
                    )
                ],
            )
    expected_purpose = "ONE_OFF_PURCHASE" if product["mechanism"] == "TERM_LOAN" else "REPEATED_PURCHASES"
    if need["purpose"] != expected_purpose:
        return stop("NOT_ELIGIBLE", [_reason("PRODUCT_PURPOSE", "Нужен другой механизм для описанной потребности")])
    checks = payload.get("source_checks", {})
    unavailable = [
        name
        for name in ("registry", "credit_history", "control")
        if checks.get(name) in {None, "TIMEOUT", "UNAVAILABLE", "PENDING", "STALE", "FAILED"}
    ]
    if unavailable:
        return stop(
            "WAITING_SOURCE",
            [_reason("SOURCE_UNAVAILABLE", "Обязательная проверка не завершена; это не кредитный отказ", sources=unavailable)],
        )
    if checks.get("registry") == "LIQUIDATED":
        return stop("NOT_ELIGIBLE", [_reason("PRODUCT_STATUS", "Реестр подтвердил ликвидацию")])
    for index, item in enumerate(payload["history"]):
        statement = item.get("statement_receipts_kopecks")
        if statement is not None:
            try:
                money(statement, "statement_receipts_kopecks")
            except CreditDomainError:
                return stop("NEEDS_INFO", [_reason("DATA_CONTRADICTORY", "Неверная сумма очищенной выписки", field=f"history.{index}")])
            declared = item["customer_receipts_kopecks"]
            if abs(declared - statement) * 10 > max(declared, statement):
                return stop(
                    "NEEDS_INFO",
                    [
                        _reason(
                            "RECONCILIATION_REQUIRED",
                            "Расхождение с очищенной выпиской превышает 10%",
                            field=f"history.{index}",
                            declared_kopecks=declared,
                            statement_kopecks=statement,
                        )
                    ],
                )
    if payload.get("target_purchase_double_counted"):
        return stop("NEEDS_INFO", [_reason("DATA_CONTRADICTORY", "Закупка учтена в начальном бюджете и в будущем CFADS дважды")])
    result["risk"] = _risk(payload, age)
    manual_reasons = []
    if any(checks.get(name) != "CLEAR" for name in ("registry", "credit_history", "control")):
        manual_reasons.append(_reason("CONTROL_REVIEW", "Нужно завершить содержательную ручную проверку источников"))
    if not payload["documents_verified"]:
        manual_reasons.append(_reason("DOCUMENT_REVIEW", "Документы вне синтетического manifest должен содержательно проверить аналитик"))
    if payload["seasonal"] or payload.get("intramonth_gap"):
        manual_reasons.append(_reason("SEASONALITY_REVIEW", "Сезонность или внутримесячные разрывы требуют ручной оценки"))
    if result["risk"]["category"] in {"B", "C"}:
        manual_reasons.append(_reason("RISK_HISTORY", "Категория риска требует ручного решения", category=result["risk"]["category"]))
    historical_average = sum(cfads(item) for item in payload["history"][-3:]) / Decimal(3)
    changed_periods = []
    for index, item in enumerate(payload["forecast"]):
        if abs(Decimal(cfads(item)) - historical_average) > abs(historical_average) * Decimal("0.20"):
            changed_periods.append(index + 1)
    if changed_periods:
        manual_reasons.append(
            _reason(
                "FORECAST_DEVIATION",
                "Прогноз CFADS отличается от среднего последних трех месяцев более чем на 20%",
                periods=changed_periods,
                historical_average_cfads_kopecks=str(historical_average),
            )
        )
    if need["total_cost_kopecks"] > amount + need["own_contribution_kopecks"]:
        manual_reasons.append(_reason("TARGET_BUDGET_GAP", "Запрошенные и подтвержденные собственные средства не покрывают задачу"))
    try:
        if product["mechanism"] == "TERM_LOAN":
            original = evaluate_term(amount, months, payload["forecast"], payload["initial_cash_kopecks"], submitted)
        else:
            original = evaluate_cycle(payload["cycle_plan"], amount, 90, submitted)
    except (CreditDomainError, KeyError) as exc:
        return stop("NEEDS_INFO", [_reason(getattr(exc, "code", "DATA_MISSING"), str(exc))])
    original["candidate_id"] = "original"
    result["original"] = original
    financial_ok = original["passed"]
    clear = all(checks.get(name) == "CLEAR" for name in ("registry", "credit_history", "control"))
    result["manual_approval_allowed"] = (
        result["risk"]["category"] != "C"
        and financial_ok
        and clear
        and payload["documents_verified"]
        and not any(reason["code"] == "TARGET_BUDGET_GAP" for reason in manual_reasons)
    )
    if manual_reasons and (
        result["risk"]["category"] == "C"
        or not clear
        or not payload["documents_verified"]
        or any(r["code"] == "TARGET_BUDGET_GAP" for r in manual_reasons)
    ):
        if not financial_ok:
            manual_reasons.append(_reason("CASHFLOW_INSUFFICIENT", "Исходный календарь не проходит финансовые правила"))
        return stop("MANUAL_REVIEW", manual_reasons)
    selected = original if financial_ok else None
    if selected is None and product["mechanism"] == "TERM_LOAN":
        for term in product["terms_months"]:
            if term <= months:
                continue
            alternative = evaluate_term(amount, term, payload["forecast"], payload["initial_cash_kopecks"], submitted)
            alternative["candidate_id"] = f"term-{amount}-{term}"
            alternative["change"] = "LONGER_TERM"
            result["alternatives"].append(alternative)
            if alternative["passed"]:
                selected = alternative
                break
        if selected is None and need["partial_feasible"]:
            floor = max(product["minimum_kopecks"], need["minimum_useful_kopecks"])
            candidate = ((amount - 1) // 1_000_000) * 1_000_000
            while candidate >= floor:
                alternative = evaluate_term(candidate, months, payload["forecast"], payload["initial_cash_kopecks"], submitted)
                alternative["candidate_id"] = f"term-{candidate}-{months}"
                alternative["change"] = "LOWER_AMOUNT"
                alternative["unfunded_need_kopecks"] = max(0, need["total_cost_kopecks"] - candidate - need["own_contribution_kopecks"])
                # Keep one best candidate, not hundreds of full calendars in an API response.
                if alternative["passed"]:
                    result["alternatives"].append(alternative)
                    selected = alternative
                    break
                candidate -= 1_000_000
    if selected is None:
        return stop(
            "MANUAL_REVIEW",
            [
                *manual_reasons,
                _reason("CASHFLOW_INSUFFICIENT", "Исходный вариант и допустимые альтернативы не покрываются подтвержденным календарем"),
            ],
        )
    changed = selected is not original
    result.update(status="OFFER_READY", decision="APPROVE_CHANGED" if changed else "APPROVE", manual_approval_allowed=True)
    result["offer"] = {
        "product_code": product["code"],
        "amount_kopecks": selected["amount_kopecks"],
        "term_months": selected.get("months", 12),
        "annual_rate_bps": 2400,
        "fees_kopecks": 0,
        "calculation": selected,
        "unfunded_need_kopecks": selected.get("unfunded_need_kopecks", 0),
        "change": selected.get("change"),
        "notice": DEMO_NOTICE,
    }
    result["reasons"] = [
        _reason(
            "APPROVE_CHANGED" if changed else "APPROVE", "Допустимые условия рассчитаны по подтвержденным данным и синтетической политике"
        )
    ]
    if manual_reasons:
        result.update(status="MANUAL_REVIEW", decision=None, reasons=manual_reasons)
    return result
