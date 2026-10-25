from copy import deepcopy

import pytest

from app.credit_policy import evaluate_application, evaluate_cycle, evaluate_term, full_months
from app.credit_servicing import CreditDomainError


def period(cfads_rubles=400_000, **extra):
    return {"customer_receipts_kopecks": 240_000_000, "operating_payments_kopecks": (2_400_000 - cfads_rubles) * 100,
            "taxes_kopecks": 0, "maintenance_capex_kopecks": 0, "mandatory_withdrawals_kopecks": 0, **extra}


def application(**changes):
    return {"product_code": "DEMO_TERM_LOAN_V1", "amount_kopecks": 200_000_000, "term_months": 12,
            "submitted_on": "2026-10-25", "organization": {"legal_form": "IP", "registration_date": "2024-04-25", "status": "ACTIVE"},
            "need": {"minimum_useful_kopecks": 200_000_000, "partial_feasible": False, "total_cost_kopecks": 200_000_000,
                     "own_contribution_kopecks": 0, "purpose": "ONE_OFF_PURCHASE"},
            "history": [period(period=f"2026-{month:02}") for month in range(4, 10)],
            "forecast": [period(old_debt_service_kopecks=10_000_000) for _ in range(24)],
            "initial_cash_kopecks": 10_000_000, "concentration_bps": 3000, "active_overdue_days": 0,
            "closed_overdue_last12": False, "seasonal": False, "financials_confirmed": True,
            "documents_complete": True, "documents_verified": True,
            "source_checks": {"registry": "CLEAR", "credit_history": "CLEAR", "control": "CLEAR"}, **changes}


def cycle_plan(**changes):
    return {"confirmed": True, "initial_cash_kopecks": 10_000_000, "events": [
        {"id": "purchase", "date": "2026-10-25", "kind": "TARGET_PAYMENT", "amount_kopecks": 100_000_000},
        {"id": "expense", "date": "2026-11-01", "kind": "OPERATING_PAYMENT", "amount_kopecks": 5_000_000},
        {"id": "sales", "date": "2027-01-23", "kind": "CUSTOMER_RECEIPT", "amount_kopecks": 130_000_000}], **changes}


def test_fix_a_auto_offer_uses_every_period_and_reproducible_snapshot():
    data = application()
    original = deepcopy(data)
    answer = evaluate_application(data)
    assert answer["status"] == "OFFER_READY" and answer["decision"] == "APPROVE"
    assert answer["risk"]["category"] == "A" and answer["risk"]["is_probability"] is False
    assert answer["original"]["periods"][0]["dscr"] == "1.3835"
    assert answer["original"]["periods"][0]["stress_dscr"] == "1.2452"
    assert answer["offer"]["calculation"]["schedule"]["regular_payment_kopecks"] == 18_911_919
    assert evaluate_application(data) == answer
    assert data == original


def test_fix_b_first_acceptable_longer_term_keeps_original_request():
    data = application(amount_kopecks=300_000_000)
    data["need"].update(minimum_useful_kopecks=300_000_000, total_cost_kopecks=300_000_000)
    data["history"] = [period(350_000, period=f"2026-{month:02}") for month in range(4, 10)]
    data["forecast"] = [period(350_000, old_debt_service_kopecks=10_000_000) for _ in range(24)]
    result = evaluate_application(data)
    assert result["status"] == "OFFER_READY" and result["decision"] == "APPROVE_CHANGED"
    assert result["original"]["months"] == 12 and result["original"]["passed"] is False
    assert [item["months"] for item in result["alternatives"]] == [18, 24]
    assert result["alternatives"][0]["passed"] is False
    offer = result["offer"]
    assert offer["term_months"] == 24 and offer["amount_kopecks"] == 300_000_000
    assert offer["calculation"]["schedule"]["interest_kopecks"] == 80_671_903
    assert data["term_months"] == 12


def test_fix_c_average_dscr_does_not_hide_seasonal_deficit():
    flows = [80_000, 80_000, 120_000, 180_000, 300_000, 420_000, 520_000, 520_000, 400_000, 300_000, 180_000, 100_000]
    data = application(amount_kopecks=150_000_000, seasonal=True)
    data["need"].update(minimum_useful_kopecks=150_000_000, total_cost_kopecks=150_000_000)
    data["forecast"] = [period(value, old_debt_service_kopecks=4_000_000) for value in flows]
    result = evaluate_application(data)
    assert result["status"] == "MANUAL_REVIEW" and result["offer"] is None
    assert result["original"]["average_dscr"] == "1.4665"
    assert result["original"]["periods"][0]["ending_cash_kopecks"] == -183_939
    assert result["original"]["failed_period_numbers"][:4] == [1, 2, 3, 4]
    assert result["manual_approval_allowed"] is False


def test_old_obligation_ends_and_is_not_repeated_for_remaining_horizon():
    forecast = [period(old_debt_service_kopecks=10_000_000 if month < 4 else 0) for month in range(12)]
    result = evaluate_term(200_000_000, 12, forecast, 10_000_000)
    assert result["periods"][3]["old_debt_service_kopecks"] == 10_000_000
    assert result["periods"][4]["old_debt_service_kopecks"] == 0
    assert result["periods"][4]["dscr"] > result["periods"][3]["dscr"]


@pytest.mark.parametrize("field,value,category", [
    ("active_overdue_days", 29, "B"), ("active_overdue_days", 30, "C"),
    ("closed_overdue_last12", True, "B"), ("concentration_bps", 5000, "A"), ("concentration_bps", 5001, "B"),
])
def test_exact_risk_boundaries_and_no_automatic_decline(field, value, category):
    result = evaluate_application(application(**{field: value}))
    assert result["risk"]["category"] == category
    assert result["status"] == ("OFFER_READY" if category == "A" else "MANUAL_REVIEW")
    assert result["decision"] != "DECLINE"
    if category == "C":
        assert result["manual_approval_allowed"] is False


def test_fix_f_product_age_has_no_bad_risk_category():
    data = application()
    data["organization"]["registration_date"] = "2026-07-25"
    result = evaluate_application(data)
    assert result["status"] == "NOT_ELIGIBLE"
    assert result["risk"]["category"] == "NOT_CALCULATED"
    assert result["reasons"][0]["code"] == "PRODUCT_AGE"
    assert full_months("2026-01-26", "2026-10-25") == 8
    assert full_months("2026-01-25", "2026-10-25") == 9


def test_fix_g_persistent_negative_cfads_c_requires_expert_not_auto_decline():
    data = application()
    data["history"] = [period(-120_000, period=f"2026-{month:02}") for month in range(4, 10)]
    data["forecast"] = [period(-120_000, old_debt_service_kopecks=0) for _ in range(24)]
    result = evaluate_application(data)
    assert result["risk"]["category"] == "C"
    assert result["status"] == "MANUAL_REVIEW" and result["manual_approval_allowed"] is False


def test_no_zero_imputation_missing_period_and_missing_number_are_specific():
    data = application()
    data["history"].pop(2)
    result = evaluate_application(data)
    assert result["status"] == "NEEDS_INFO"
    assert result["risk"]["category"] == "NOT_CALCULATED"
    assert result["reasons"][0]["expected_periods"] == [f"2026-{month:02}" for month in range(4, 10)]
    data = application()
    data["forecast"][0]["taxes_kopecks"] = None
    result = evaluate_application(data)
    assert result["status"] == "NEEDS_INFO"
    assert result["reasons"][0]["field"] == "forecast.0.taxes_kopecks"


def test_reconciliation_over_ten_percent_and_both_zero_boundary():
    data = application()
    data["history"][0]["statement_receipts_kopecks"] = 300_000_000
    result = evaluate_application(data)
    assert result["status"] == "NEEDS_INFO" and result["reasons"][0]["code"] == "RECONCILIATION_REQUIRED"
    data["history"][0]["statement_receipts_kopecks"] = 240_000_000
    assert evaluate_application(data)["status"] == "OFFER_READY"
    data["history"] = [period(0, period=f"2026-{month:02}", customer_receipts_kopecks=0,
                              operating_payments_kopecks=0, statement_receipts_kopecks=0) for month in range(4, 10)]
    assert not any(reason["code"] == "RECONCILIATION_REQUIRED" for reason in evaluate_application(data)["reasons"])


def test_timeout_stale_unknown_documents_and_control_are_not_economic_refusals():
    for source_result in ["TIMEOUT", "STALE", "PENDING"]:
        data = application()
        data["source_checks"]["credit_history"] = source_result
        result = evaluate_application(data)
        assert result["status"] == "WAITING_SOURCE" and result["risk"]["category"] == "NOT_CALCULATED"
    for data in [application(documents_verified=False), application(source_checks={
        "registry": "CLEAR", "credit_history": "CLEAR", "control": "REVIEW_REQUIRED"})]:
        result = evaluate_application(data)
        assert result["status"] == "MANUAL_REVIEW" and result["manual_approval_allowed"] is False


def test_unconfirmed_growth_and_double_counted_target_never_auto_approve():
    data = application(financials_confirmed=False)
    assert evaluate_application(data)["status"] == "NEEDS_INFO"
    data = application()
    data["forecast"][0] = period(480_001, old_debt_service_kopecks=10_000_000)
    result = evaluate_application(data)
    assert result["status"] == "MANUAL_REVIEW"
    assert any(reason["code"] == "FORECAST_DEVIATION" for reason in result["reasons"])
    assert evaluate_application(application(target_purchase_double_counted=True))["status"] == "NEEDS_INFO"


def test_alternative_lower_amount_step_and_minimum_useful_not_fake_solution():
    data = application(amount_kopecks=500_000_000, term_months=24)
    data["need"].update(minimum_useful_kopecks=30_000_000, partial_feasible=True, total_cost_kopecks=500_000_000)
    result = evaluate_application(data)
    assert result["decision"] == "APPROVE_CHANGED"
    amount = result["offer"]["amount_kopecks"]
    assert amount % 1_000_000 == 0 and amount < 500_000_000
    assert result["offer"]["unfunded_need_kopecks"] == 500_000_000 - amount
    # Maximal accepted step, verified independently by running the next larger amount.
    assert evaluate_term(amount + 1_000_000, 24, data["forecast"], data["initial_cash_kopecks"])["passed"] is False
    data["need"].update(minimum_useful_kopecks=500_000_000, partial_feasible=False)
    assert evaluate_application(data)["status"] == "MANUAL_REVIEW"


def test_short_forecast_does_not_invent_extra_months_for_alternative():
    data = application(amount_kopecks=300_000_000)
    data["need"].update(minimum_useful_kopecks=300_000_000, total_cost_kopecks=300_000_000)
    data["forecast"] = data["forecast"][:12]
    result = evaluate_application(data)
    assert result["status"] == "MANUAL_REVIEW"
    assert all(item["passed"] is False for item in result["alternatives"])
    assert result["alternatives"][0]["code"] == "DATA_MISSING"


def test_fix_d_line_full_limit_ninety_days_and_no_fictional_interest_only_coverage():
    result = evaluate_cycle(cycle_plan(), 100_000_000, 90, "2026-10-25")
    assert result["passed"] is True
    assert result["repayment_kopecks"] > 100_000_000
    data = application(product_code="DEMO_REVOLVING_V1", amount_kopecks=100_000_000, cycle_plan=cycle_plan())
    data["need"].update(minimum_useful_kopecks=100_000_000, total_cost_kopecks=100_000_000, purpose="REPEATED_PURCHASES")
    evaluated = evaluate_application(data)
    assert evaluated["status"] == "OFFER_READY" and evaluated["offer"]["term_months"] == 12
    plan = cycle_plan()
    plan["events"][2]["amount_kopecks"] = 20_000_000  # Many times the interest, but not the body.
    assert evaluate_cycle(plan, 100_000_000, 90, "2026-10-25")["passed"] is False


def test_line_cannot_use_money_after_maturity_or_hide_intraday_negative_cash():
    plan = cycle_plan()
    plan["events"][2]["date"] = "2027-01-24"
    result = evaluate_cycle(plan, 100_000_000, 90, "2026-10-25")
    assert result["passed"] is False and "MATURITY_MISMATCH" in result["reasons"]
    plan = cycle_plan(initial_cash_kopecks=0)
    result = evaluate_cycle(plan, 100_000_000, 90, "2026-10-25")
    assert result["coverage"] > "1.10" and result["liquidity_passed"] is False


def test_active_line_debt_is_kept_and_cashflow_ids_cannot_be_used_twice():
    plan = cycle_plan(existing_repayments=[{"id": "old-tranche", "date": "2026-12-01", "total_kopecks": 80_000_000}])
    result = evaluate_cycle(plan, 100_000_000, 90, "2026-10-25")
    assert result["passed"] is False
    assert result["total_return_kopecks"] == result["repayment_kopecks"] + 80_000_000
    assert any(event["id"] == "old-tranche" for event in result["calendar"])
    plan = cycle_plan()
    plan["events"].append(deepcopy(plan["events"][2]))
    with pytest.raises(CreditDomainError) as error:
        evaluate_cycle(plan, 100_000_000, 90, "2026-10-25")
    assert error.value.code == "DUPLICATE_CASHFLOW"


def test_risk_variation_is_population_not_sample_and_reasons_are_preserved():
    data = application(active_overdue_days=30, concentration_bps=9000)
    for i, item in enumerate(data["history"]):
        item["customer_receipts_kopecks"] = 1 if i < 5 else 100_000_000
    result = evaluate_application(data)
    assert result["risk"]["category"] == "C"
    codes = [reason["code"] for reason in result["risk"]["reasons"]]
    assert "RISK_ACTIVE_OVERDUE_30" in codes and "RISK_CONCENTRATION" in codes and "RISK_VOLATILE_RECEIPTS" in codes
