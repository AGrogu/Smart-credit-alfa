"""Pure monetary algorithms for the synthetic DEMO_POLICY_1 contracts.

No clock, database, network or binary floating point is used here. Callers own
authorization, versions, transaction locks, ledger entries and idempotency.
All monetary API values are integer kopecks; fractional accrual is a string.
"""

from calendar import monthrange
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext


class CreditDomainError(ValueError):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.details = details


def money(value, field="amount_kopecks", *, positive=False):
    if isinstance(value, bool) or not isinstance(value, int) or value < (1 if positive else 0):
        raise CreditDomainError("INVALID_MONEY", "Сумма должна быть целым числом копеек", field=field)
    return value


def calendar_date(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) else value if type(value) is date else date.fromisoformat("")
    except (TypeError, ValueError):
        raise CreditDomainError("INVALID_DATE", "Нужна календарная дата YYYY-MM-DD") from None


def rounded(value):
    return int(Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def add_months(iso_date: str, months: int) -> str:
    """Add from the original day, never from an already shortened February."""
    original = calendar_date(iso_date)
    if isinstance(months, bool) or not isinstance(months, int):
        raise CreditDomainError("INVALID_TERM", "Срок должен быть целым числом месяцев")
    absolute = original.year * 12 + original.month - 1 + months
    year, zero_month = divmod(absolute, 12)
    month = zero_month + 1
    return date(year, month, min(original.day, monthrange(year, month)[1])).isoformat()


def annuity_schedule(principal_kopecks: int, months: int, issued_on: str, annual_rate_bps: int = 2400) -> dict:
    principal = money(principal_kopecks, positive=True)
    if isinstance(months, bool) or not isinstance(months, int) or not 1 <= months <= 600:
        raise CreditDomainError("INVALID_TERM", "Недопустимый срок графика")
    money(annual_rate_bps, "annual_rate_bps")
    issued_on = calendar_date(issued_on).isoformat()
    with localcontext() as ctx:
        ctx.prec = 50
        rate = Decimal(annual_rate_bps) / 120000
        regular = rounded(Decimal(principal) * rate / (1 - (1 + rate) ** -months)) if rate else rounded(Decimal(principal) / months)
        remaining = principal
        rows = []
        for number in range(1, months + 1):
            interest = rounded(Decimal(remaining) * rate)
            paid_principal = remaining if number == months else min(remaining, regular - interest)
            if paid_principal < 0:
                raise CreditDomainError("INVALID_SCHEDULE", "Платеж не покрывает проценты")
            opening = remaining
            remaining -= paid_principal
            rows.append({"number": number, "due_on": add_months(issued_on, number),
                         "opening_principal_kopecks": opening, "payment_kopecks": paid_principal + interest,
                         "interest_kopecks": interest, "principal_kopecks": paid_principal,
                         "remaining_principal_kopecks": remaining, "status": "SCHEDULED"})
    total = sum(row["payment_kopecks"] for row in rows)
    return {"principal_kopecks": principal, "annual_rate_bps": annual_rate_bps, "months": months,
            "issued_on": issued_on, "matures_on": rows[-1]["due_on"], "regular_payment_kopecks": regular,
            "last_payment_kopecks": rows[-1]["payment_kopecks"], "total_kopecks": total,
            "interest_kopecks": total - principal, "rows": rows,
            "convention": "MONTHLY_ANNUITY_HALF_UP_LAST_ADJUSTED", "holiday_adjustment": False}


def act365_interest(principal_kopecks: int, days: int, annual_rate_bps: int = 2400) -> dict:
    money(principal_kopecks)
    money(annual_rate_bps, "annual_rate_bps")
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        raise CreditDomainError("INVALID_DAYS", "Число дней должно быть неотрицательным")
    with localcontext() as ctx:
        ctx.prec = 50
        exact = Decimal(principal_kopecks) * annual_rate_bps * days / Decimal(3650000)
        return {"days": days, "interest_kopecks": rounded(exact), "unrounded_interest_kopecks": str(exact),
                "day_count": "ACT/365 Fixed"}


def tranche_quote(tranche: dict, on_date: str) -> dict:
    principal = money(tranche["principal_kopecks"])
    issued = calendar_date(tranche["issued_on"])
    anchor = calendar_date(tranche.get("interest_anchor_on", tranche["issued_on"]))
    today = calendar_date(on_date)
    if today < anchor or anchor < issued:
        raise CreditDomainError("DATE_BEFORE_ACCRUAL", "Расчетная дата предшествует начислению")
    try:
        accumulator = Decimal(tranche.get("accrued_unrounded_kopecks", "0"))
    except (InvalidOperation, TypeError):
        raise CreditDomainError("INVALID_ACCRUAL", "Некорректный аккумулятор процентов") from None
    if not accumulator.is_finite() or accumulator < 0:
        raise CreditDomainError("INVALID_ACCRUAL", "Некорректный аккумулятор процентов")
    accrued = act365_interest(principal, (today - anchor).days, tranche.get("annual_rate_bps", 2400))
    with localcontext() as ctx:
        ctx.prec = 50
        exact = accumulator + Decimal(accrued["unrounded_interest_kopecks"])
        interest = rounded(exact)
    return {"tranche_id": tranche.get("id"), "on_date": today.isoformat(), "principal_kopecks": principal,
            "interest_kopecks": interest, "total_kopecks": principal + interest,
            "unrounded_interest_kopecks": str(exact), "accrual_from": anchor.isoformat(),
            "days": (today - anchor).days, "day_count": "ACT/365 Fixed"}


def repay_tranche(tranche: dict, payment_kopecks: int, on_date: str, account_balance_kopecks: int | None = None) -> dict:
    amount = money(payment_kopecks, positive=True)
    if tranche.get("status", "ACTIVE") not in {"ACTIVE", "OVERDUE"}:
        raise CreditDomainError("TRANCHE_NOT_ACTIVE", "У транша нет доступного для погашения долга")
    if calendar_date(on_date) <= calendar_date(tranche["issued_on"]):
        raise CreditDomainError("REPAYMENT_TOO_EARLY", "Возврат разрешен со следующего дня после выдачи")
    quote = tranche_quote(tranche, on_date)
    if amount < quote["interest_kopecks"]:
        raise CreditDomainError("AMOUNT_BELOW_INTEREST", "Сначала нужно полностью оплатить начисленные проценты")
    if amount > quote["total_kopecks"]:
        raise CreditDomainError("AMOUNT_EXCEEDS_DEBT", "Сумма превышает полный долг")
    if account_balance_kopecks is not None and money(account_balance_kopecks) < amount:
        raise CreditDomainError("INSUFFICIENT_FUNDS", "Недостаточно средств на синтетическом счете")
    paid_principal = amount - quote["interest_kopecks"]
    result = deepcopy(tranche)
    result.update(principal_kopecks=quote["principal_kopecks"] - paid_principal,
                  interest_anchor_on=calendar_date(on_date).isoformat(), accrued_unrounded_kopecks="0")
    result["status"] = "REPAID" if result["principal_kopecks"] == 0 else (
        "OVERDUE" if calendar_date(on_date) > calendar_date(tranche["matures_on"]) else "ACTIVE")
    return {"tranche": result, "quote": quote, "payment": {"total_kopecks": amount,
            "interest_kopecks": quote["interest_kopecks"], "principal_kopecks": paid_principal,
            "on_date": calendar_date(on_date).isoformat()}}


def facility_summary(facility: dict, tranches: list[dict], on_date: str) -> dict:
    limit = money(facility["limit_kopecks"], positive=True)
    today = calendar_date(on_date)
    expiry = calendar_date(facility["expires_on"])
    principal = reserved = interest = slots = 0
    overdue = []
    for tranche in tranches:
        state = tranche["status"]
        if state in {"RESERVED", "DISBURSING"}:
            reserved += money(tranche["amount_kopecks"], positive=True)
            slots += 1
        elif state in {"ACTIVE", "OVERDUE"}:
            principal += money(tranche["principal_kopecks"])
            interest += tranche_quote(tranche, on_date)["interest_kopecks"]
            slots += 1
            if today > calendar_date(tranche["matures_on"]) and tranche["principal_kopecks"]:
                overdue.append(tranche)
    if principal + reserved > limit:
        raise CreditDomainError("LIMIT_INVARIANT", "Использование и резервы превышают лимит")
    status = facility.get("status", "ACTIVE")
    if status != "CLOSED":
        status = "EXPIRED" if today > expiry else "BLOCKED" if overdue or not facility.get("control_clear", True) else "ACTIVE"
        if today > expiry and principal == reserved == interest == 0:
            status = "CLOSED"
    raw_available = limit - principal - reserved
    earliest = min(overdue, key=lambda t: (t["matures_on"], str(t.get("id", "")))) if overdue else None
    return {"status": status, "limit_kopecks": limit, "principal_kopecks": principal, "reserved_kopecks": reserved,
            "available_kopecks": raw_available if status == "ACTIVE" else 0, "unused_kopecks": raw_available,
            "interest_kopecks": interest, "occupied_slots": slots, "expires_on": expiry.isoformat(),
            "earliest_overdue_tranche_id": earliest.get("id") if earliest else None,
            "overdue_tranche_ids": [item.get("id") for item in sorted(overdue, key=lambda t: t["matures_on"])]}


def validate_draw(facility: dict, tranches: list[dict], amount_kopecks: int, days: int, on_date: str,
                  checks_fresh: bool = True, control_clear: bool = True) -> dict:
    amount = money(amount_kopecks, positive=True)
    summary = facility_summary(facility, tranches, on_date)
    if summary["status"] != "ACTIVE":
        raise CreditDomainError("FACILITY_NOT_ACTIVE", "Новая выдача по линии недоступна", status=summary["status"])
    if not checks_fresh:
        raise CreditDomainError("CHECKS_STALE", "Нужно обновить проверки перед новым подтверждением")
    if not control_clear:
        raise CreditDomainError("CONTROL_REVIEW", "Нужно завершить дополнительную проверку")
    if amount < 5_000_000:
        raise CreditDomainError("TRANCHE_MINIMUM", "Минимальный транш — 50 000 рублей")
    if amount > summary["available_kopecks"]:
        raise CreditDomainError("LIMIT_INSUFFICIENT", "Недостаточно доступного лимита")
    if summary["occupied_slots"] >= 3:
        raise CreditDomainError("TRANCHE_SLOTS_EXHAUSTED", "Одновременно допускается не более трех непогашенных траншей")
    if isinstance(days, bool) or days not in {30, 60, 90}:
        raise CreditDomainError("INVALID_TRANCHE_TERM", "Срок транша — 30, 60 или 90 дней")
    maturity = (calendar_date(on_date) + timedelta(days=days)).isoformat()
    if maturity > facility["expires_on"]:
        raise CreditDomainError("MATURITY_MISMATCH", "Транш должен вернуться до окончания линии")
    return {"amount_kopecks": amount, "days": days, "issued_on": calendar_date(on_date).isoformat(),
            "matures_on": maturity, "available_after_reservation_kopecks": summary["available_kopecks"] - amount}


def validate_repayment_order(tranche_id: str, facility: dict, tranches: list[dict], on_date: str) -> None:
    earliest = facility_summary(facility, tranches, on_date)["earliest_overdue_tranche_id"]
    if earliest is not None and tranche_id != earliest:
        raise CreditDomainError("EARLIEST_OVERDUE_FIRST", "Сначала нужно погасить самый ранний просроченный транш")


def loan_repayment_quote(schedule: dict, on_date: str) -> dict:
    today = calendar_date(on_date)
    unpaid = [row for row in schedule["rows"] if row.get("status") != "PAID"]
    if not unpaid:
        raise CreditDomainError("LOAN_CLOSED", "Кредит полностью погашен")
    row = min(unpaid, key=lambda item: item["number"])
    due = calendar_date(row["due_on"])
    if today < due:
        raise CreditDomainError("PAYMENT_NOT_DUE", "Досрочное погашение разового кредита не входит в демомодель")
    return {"row_number": row["number"], "on_date": today.isoformat(), "due_on": row["due_on"],
            "principal_kopecks": row["principal_kopecks"], "interest_kopecks": row["interest_kopecks"],
            "total_kopecks": row["payment_kopecks"], "overdue_days": (today - due).days}


def repay_loan_row(schedule: dict, row_number: int, payment_kopecks: int, on_date: str, account_balance_kopecks: int) -> dict:
    quote = loan_repayment_quote(schedule, on_date)
    if row_number != quote["row_number"]:
        raise CreditDomainError("EARLIEST_PAYMENT_FIRST", "Нужно погасить наиболее раннюю наступившую строку")
    if money(payment_kopecks, positive=True) != quote["total_kopecks"]:
        raise CreditDomainError("FULL_SCHEDULE_ROW_REQUIRED", "В демомодели оплачивается полная наступившая строка")
    if money(account_balance_kopecks) < payment_kopecks:
        raise CreditDomainError("INSUFFICIENT_FUNDS", "Недостаточно средств на синтетическом счете")
    result = deepcopy(schedule)
    for row in result["rows"]:
        if row["number"] == row_number:
            row.update(status="PAID", paid_on=calendar_date(on_date).isoformat())
    outstanding = sum(row["principal_kopecks"] for row in result["rows"] if row["status"] != "PAID")
    return {"schedule": result, "payment": quote, "remaining_principal_kopecks": outstanding,
            "status": "CLOSED" if outstanding == 0 else "ACTIVE"}


def schedule_as_of(schedule: dict, on_date: str) -> dict:
    result = deepcopy(schedule)
    today = calendar_date(on_date)
    for row in result["rows"]:
        if row.get("status") != "PAID":
            due = calendar_date(row["due_on"])
            row["status"] = "OVERDUE" if today > due else "DUE" if today == due else "SCHEDULED"
            row["overdue_days"] = max(0, (today - due).days)
    return result
