from copy import deepcopy

import pytest

from app.credit_servicing import (
    CreditDomainError, act365_interest, add_months, annuity_schedule, facility_summary,
    loan_repayment_quote, repay_loan_row, repay_tranche, schedule_as_of, tranche_quote,
    validate_draw, validate_repayment_order,
)


def tranche(**changes):
    return {"id": "t1", "amount_kopecks": 60_000_000, "principal_kopecks": 60_000_000,
            "issued_on": "2026-10-25", "matures_on": "2026-11-24", "annual_rate_bps": 2400,
            "interest_anchor_on": "2026-10-25", "accrued_unrounded_kopecks": "0", "status": "ACTIVE", **changes}


def facility(**changes):
    return {"limit_kopecks": 100_000_000, "expires_on": "2027-10-25", "status": "ACTIVE", **changes}


def test_annuity_tz_exact_control_examples_and_last_payment():
    schedule = annuity_schedule(200_000_000, 12, "2026-10-25")
    assert schedule["regular_payment_kopecks"] == 18_911_919
    assert schedule["last_payment_kopecks"] == 18_911_921
    assert schedule["total_kopecks"] == 226_943_030
    assert schedule["interest_kopecks"] == 26_943_030
    assert sum(row["principal_kopecks"] for row in schedule["rows"]) == 200_000_000
    assert schedule["rows"][-1]["remaining_principal_kopecks"] == 0
    assert schedule["rows"][0]["interest_kopecks"] == 4_000_000
    assert schedule["rows"][0]["principal_kopecks"] == 14_911_919
    for months, payment, final, interest in [(12, 28_367_879, 28_367_878, 40_414_547),
                                             (24, 15_861_329, 15_861_336, 80_671_903)]:
        result = annuity_schedule(300_000_000, months, "2026-10-25")
        assert result["regular_payment_kopecks"] == payment
        assert result["last_payment_kopecks"] == final
        assert result["interest_kopecks"] == interest


def test_calendar_anchors_january_31_and_leap_february():
    assert add_months("2027-01-31", 1) == "2027-02-28"
    assert add_months("2028-01-31", 1) == "2028-02-29"
    assert [row["due_on"] for row in annuity_schedule(30_000_000, 3, "2028-01-31")["rows"]] == [
        "2028-02-29", "2028-03-31", "2028-04-30"]


@pytest.mark.parametrize("principal,months", [(30_000_001, 6), (499_999_999, 24), (1, 12), (101, 24)])
def test_every_kopeck_is_repaid_without_negative_balance(principal, months):
    schedule = annuity_schedule(principal, months, "2026-01-31")
    assert sum(row["principal_kopecks"] for row in schedule["rows"]) == principal
    assert all(row["remaining_principal_kopecks"] >= 0 for row in schedule["rows"])


def test_act365_partial_interest_payment_and_limit_restoration_tz():
    original = tranche()
    first = repay_tranche(original, 10_394_521, "2026-11-04", 20_000_000)
    assert first["payment"]["interest_kopecks"] == 394_521
    assert first["payment"]["principal_kopecks"] == 10_000_000
    assert first["tranche"]["principal_kopecks"] == 50_000_000
    assert original["principal_kopecks"] == 60_000_000
    assert facility_summary(facility(), [first["tranche"]], "2026-11-04")["available_kopecks"] == 50_000_000
    full = tranche_quote(first["tranche"], "2026-11-24")
    assert full["interest_kopecks"] == 657_534
    assert full["total_kopecks"] == 50_657_534
    second = repay_tranche(first["tranche"], full["total_kopecks"], "2026-11-24")
    assert second["tranche"]["status"] == "REPAID"
    assert first["payment"]["interest_kopecks"] + second["payment"]["interest_kopecks"] == 1_052_055
    assert facility_summary(facility(), [second["tranche"]], "2026-11-24")["available_kopecks"] == 100_000_000


def test_act365_fixed_leap_year_and_no_rounding_per_day():
    assert act365_interest(100_000_000, 30)["interest_kopecks"] == 1_972_603
    value = tranche(issued_on="2028-02-28", interest_anchor_on="2028-02-28", matures_on="2028-03-29")
    assert tranche_quote(value, "2028-03-01")["days"] == 2
    daily = act365_interest(1, 1)["unrounded_interest_kopecks"]
    assert daily != "0"
    # Reading/refreshing never capitalizes, moves anchors or rounds accumulators.
    before = deepcopy(value)
    assert tranche_quote(value, "2028-03-01") == tranche_quote(value, "2028-03-01")
    assert value == before


def test_interest_only_does_not_restore_principal_or_limit():
    original = tranche()
    due_interest = tranche_quote(original, "2026-11-04")["interest_kopecks"]
    paid = repay_tranche(original, due_interest, "2026-11-04")["tranche"]
    assert paid["principal_kopecks"] == 60_000_000
    assert facility_summary(facility(), [paid], "2026-11-04")["available_kopecks"] == 40_000_000
    assert tranche_quote(paid, "2026-11-04")["interest_kopecks"] == 0


@pytest.mark.parametrize("amount,on_date,balance,code", [
    (1, "2026-10-25", None, "REPAYMENT_TOO_EARLY"),
    (1, "2026-11-04", None, "AMOUNT_BELOW_INTEREST"),
    (61_000_000, "2026-11-04", None, "AMOUNT_EXCEEDS_DEBT"),
    (10_394_521, "2026-11-04", 10_394_520, "INSUFFICIENT_FUNDS"),
])
def test_invalid_tranche_payments_are_pure_and_specific(amount, on_date, balance, code):
    original = tranche()
    before = deepcopy(original)
    with pytest.raises(CreditDomainError) as error:
        repay_tranche(original, amount, on_date, balance)
    assert error.value.code == code
    assert original == before


def test_expiry_and_overdue_keep_debt_and_block_new_draw():
    assert facility_summary(facility(), [], "2026-10-25")["status"] == "ACTIVE"
    active = tranche()
    due = facility_summary(facility(), [active], "2026-11-24")
    assert due["status"] == "ACTIVE"
    overdue = facility_summary(facility(), [active], "2026-11-25")
    assert overdue["status"] == "BLOCKED" and overdue["principal_kopecks"] == 60_000_000
    expired = facility_summary(facility(), [active], "2027-10-26")
    assert expired["status"] == "EXPIRED" and expired["interest_kopecks"] > overdue["interest_kopecks"]
    assert facility_summary(facility(), [], "2027-10-26")["status"] == "CLOSED"
    assert facility_summary(facility(), [], "2027-10-25")["status"] == "ACTIVE"


def test_reservations_count_against_money_and_slots():
    reserved = {"id": "r1", "status": "RESERVED", "amount_kopecks": 80_000_000}
    view = facility_summary(facility(), [reserved], "2026-10-25")
    assert view["reserved_kopecks"] == 80_000_000 and view["available_kopecks"] == 20_000_000
    with pytest.raises(CreditDomainError, match="Недостаточно") as error:
        validate_draw(facility(), [reserved], 80_000_000, 30, "2026-10-25")
    assert error.value.code == "LIMIT_INSUFFICIENT"
    slots = [{"id": str(i), "status": "RESERVED", "amount_kopecks": 5_000_000} for i in range(3)]
    with pytest.raises(CreditDomainError) as error:
        validate_draw(facility(), slots, 5_000_000, 30, "2026-10-25")
    assert error.value.code == "TRANCHE_SLOTS_EXHAUSTED"


@pytest.mark.parametrize("amount,days,on_date,fresh,code", [
    (4_999_999, 30, "2026-10-25", True, "TRANCHE_MINIMUM"),
    (5_000_000, 29, "2026-10-25", True, "INVALID_TRANCHE_TERM"),
    (5_000_000, 30, "2027-10-01", True, "MATURITY_MISMATCH"),
    (5_000_000, 30, "2026-10-25", False, "CHECKS_STALE"),
])
def test_draw_edge_codes(amount, days, on_date, fresh, code):
    with pytest.raises(CreditDomainError) as error:
        validate_draw(facility(), [], amount, days, on_date, fresh)
    assert error.value.code == code


def test_oldest_overdue_tranche_must_be_paid_first():
    first = tranche(id="earlier", matures_on="2026-11-24", principal_kopecks=40_000_000)
    second = tranche(id="later", matures_on="2026-12-24", principal_kopecks=40_000_000)
    with pytest.raises(CreditDomainError) as error:
        validate_repayment_order("later", facility(), [second, first], "2027-01-01")
    assert error.value.code == "EARLIEST_OVERDUE_FIRST"
    validate_repayment_order("earlier", facility(), [second, first], "2027-01-01")


def test_term_full_row_only_due_dates_overdue_and_close():
    schedule = annuity_schedule(200_000_000, 12, "2026-10-25")
    with pytest.raises(CreditDomainError) as error:
        loan_repayment_quote(schedule, "2026-11-24")
    assert error.value.code == "PAYMENT_NOT_DUE"
    assert schedule_as_of(schedule, "2026-11-25")["rows"][0]["status"] == "DUE"
    assert schedule_as_of(schedule, "2026-11-26")["rows"][0]["status"] == "OVERDUE"
    paid = repay_loan_row(schedule, 1, 18_911_919, "2026-11-25", 18_911_919)
    assert paid["remaining_principal_kopecks"] == 185_088_081
    assert paid["payment"]["interest_kopecks"] == 4_000_000
    with pytest.raises(CreditDomainError):
        repay_loan_row(paid["schedule"], 1, 18_911_919, "2026-12-25", 99_000_000)
    current = paid["schedule"]
    for row in schedule["rows"][1:]:
        paid = repay_loan_row(current, row["number"], row["payment_kopecks"], row["due_on"], 99_000_000)
        current = paid["schedule"]
    assert paid["status"] == "CLOSED"
    assert paid["remaining_principal_kopecks"] == 0


def test_binary_floats_and_booleans_cannot_enter_monetary_algorithms():
    for invalid in [100.0, True, "100", -1]:
        with pytest.raises(CreditDomainError):
            annuity_schedule(invalid, 12, "2026-01-01")
