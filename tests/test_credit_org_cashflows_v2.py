"""Organization-level cash sources remain single across independent contracts."""
from copy import deepcopy
from app.db import SessionLocal
from app.v2_models import CreditRecord
from conftest import login
from test_credit_cycle_v2 import fresh_credit_cycle  # noqa: F401 — autouse isolated DB fixture
from test_credit_cycle_v2 import (prepared, save, command, wait_status, accept,
    success, API, advance, workspace, active_contract, contract_command, draw_cycle, wait_contract)


def small_loan(client):
    a = prepared(client)
    need = deepcopy(a["input"]["need"])
    need.update(minimum_useful_kopecks=30_000_000, total_cost_kopecks=30_000_000)
    a = save(client, a, {"amount_kopecks":30_000_000, "need":need})
    a = command(client, a, "submit")
    a = wait_status(client, a, {"OFFER_READY"})
    a = accept(client, a)
    a = command(client, a, "sign", confirmed=True, bundle_hash=a["bundle"]["hash"])
    a = wait_status(client, a, {"CONTRACT_ACTIVE"})
    return success(client.get(API + "/contracts/" + a["contract_id"]))


def test_two_loans_share_one_monthly_business_cashflow(client):
    login(client)
    first = small_loan(client)
    second = small_loan(client)
    assert first["id"] != second["id"]
    advance(client, 31)
    account = workspace(client)["account"]
    receipts = [o for o in account["operations"] if o["kind"] == "CUSTOMER_RECEIPT"]
    expenses = [o for o in account["operations"] if o["kind"] == "OPERATING_PAYMENT"]
    old_debt = [o for o in account["operations"] if o["kind"] == "OLD_DEBT_PAYMENT"]
    assert len(receipts) == len(expenses) == len(old_debt) == 1
    assert receipts[0]["amount_kopecks"] == 240_000_000
    assert account["balance_kopecks"] == 40_000_000


def test_changed_shared_forecast_requires_clarification(client):
    login(client)
    small_loan(client)
    a = prepared(client)
    forecast = deepcopy(a["input"]["forecast"])
    forecast[0]["customer_receipts_kopecks"] += 1
    a = save(client, a, {"forecast":forecast})
    a = command(client, a, "submit")
    a = wait_status(client, a, {"NEEDS_INFO"})
    assert a["assessment"]["reasons"][0]["code"] == "CASHFLOW_CONFLICT"
    assert a["offer"] is None


def test_two_facilities_cannot_allocate_one_source_twice(client):
    login(client, "fix-d")
    first = active_contract(client, "D")
    on = workspace(client, "D")["clock"]["date"]
    plan = draw_cycle(on, amount=60_000_000)
    q = success(contract_command(client, first, "draw_preview", amount_kopecks=60_000_000, days=30, cycle_plan=plan))["quote"]
    first = success(contract_command(client, first, "draw", quote_id=q["id"], confirmed=True))
    first = wait_contract(client, first, lambda c:c["facility"]["drawn_kopecks"] == 60_000_000)
    # A second existing facility belongs to the same organization, with a distinct
    # contract/limit. The economic check must still see the first one's debt.
    with SessionLocal() as db:
        original = db.get(CreditRecord, first["id"])
        other = deepcopy(original.data)
        other.update(id="test-second-facility", tranches=[], operations=[], cash_events=[])
        db.add(CreditRecord(id=other["id"], kind="contract", organization_id=original.organization_id, revision=1, data=other))
        db.commit()
    second = success(client.get(API + "/contracts/test-second-facility"))
    response = contract_command(client, second, "draw_preview", amount_kopecks=60_000_000, days=30, cycle_plan=plan)
    assert response.status_code == 422, response.text
    assert response.headers["X-Error-Code"] == "CYCLE_CASHFLOW_INSUFFICIENT"
    changed = deepcopy(plan)
    changed["events"][2]["amount_kopecks"] += 1
    response = contract_command(client, second, "draw_preview", amount_kopecks=60_000_000, days=30, cycle_plan=changed)
    assert response.status_code == 409
    assert response.headers["X-Error-Code"] == "CASHFLOW_CONFLICT"


def test_legacy_executed_source_is_not_credited_again_after_restart_shape(client):
    login(client)
    first = small_loan(client)
    advance(client, 31)
    before = workspace(client)["account"]["balance_kopecks"]
    with SessionLocal() as db:
        row = db.get(CreditRecord, first["id"])
        legacy = deepcopy(row.data)
        for index, event in enumerate(legacy["cash_events"]):
            event.pop("cash_source", None)
            event.pop("period", None)
            event["id"] = f"{row.id}:{index}:{event['kind']}"
        row.data = legacy
        alias = deepcopy(legacy)
        alias["id"] = "test-legacy-second-contract"
        for index, event in enumerate(alias["cash_events"]):
            event["id"] = f"{alias['id']}:{index}:{event['kind']}"
            event["executed"] = False
        db.add(CreditRecord(id=alias["id"], kind="contract", organization_id=row.organization_id, revision=1, data=alias))
        db.commit()
    advance(client, 1)
    account = workspace(client)["account"]
    assert account["balance_kopecks"] == before
    assert len([o for o in account["operations"] if o["kind"] == "CUSTOMER_RECEIPT"]) == 1
