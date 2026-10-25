"""Acceptance assertions against the persistent v2 API, using only the test DB.

The conftest DATABASE_URL points to a disposable SQLite DB or explicitly selected
TEST_DATABASE_URL. Never point the latter at the demonstration database.
"""

from copy import deepcopy
import time
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models import Idempotency
from app.v2_models import CreditBlob, CreditRecord
from app.v2_seed import seed_v2
from conftest import login


API = "/api/v2"


@pytest.fixture(autouse=True)
def fresh_credit_cycle(client):
    """Reset this module's synthetic aggregates, never the legacy/demo tables."""
    with SessionLocal() as db:
        db.execute(delete(CreditBlob))
        db.execute(delete(CreditRecord))
        db.execute(delete(Idempotency).where(Idempotency.scope.like("v2:%")))
        seed_v2(db)
        db.commit()
    yield


def headers(key=None, revision=None):
    result = {"Idempotency-Key": key or str(uuid4())}
    if revision is not None:
        result["If-Match"] = str(revision)
    return result


def success(response, statuses=(200, 201, 202)):
    assert response.status_code in statuses, (response.status_code, response.text)
    return response.json()


def error(response, status, code=None):
    assert response.status_code == status, response.text
    if code:
        assert response.headers.get("X-Error-Code") == code, response.text


def workspace(client, letter="A"):
    return success(client.get(API + "/workspace", params={"organization_id": "demo-org-" + letter.lower()}))


def create(client, letter="A", key=None):
    return success(
        client.post(
            API + "/applications",
            json={"template_id": "FIX-" + letter, "organization_id": "demo-org-" + letter.lower()},
            headers=headers(key),
        )
    )


def get_application(client, application_id):
    return success(client.get(API + "/applications/" + application_id))


def command_response(client, application, action, key=None, **extra):
    return client.post(
        API + "/applications/" + application["id"] + "/commands",
        json={"action": action, "expected_version": application["revision"], **extra},
        headers=headers(key, application["revision"]),
    )


def command(client, application, action, **extra):
    return success(command_response(client, application, action, **extra))


def save(client, application, changes=None):
    value = deepcopy(application["input"])
    value.update(changes or {})
    return success(
        client.put(
            API + "/applications/" + application["id"],
            json={"expected_version": application["revision"], "input": value},
            headers=headers(revision=application["revision"]),
        )
    )


def prepared(client, letter="A"):
    application = create(client, letter)
    application = save(
        client, application, {"financials_confirmed": True, "cycle_plan": {**application["input"]["cycle_plan"], "confirmed": True}}
    )
    for purpose in ("CREDIT_HISTORY", "DATA_PROCESSING"):
        application = command(client, application, "consent", purpose=purpose, confirmed=True)
    return application


def wait_status(client, application, statuses, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        application = get_application(client, application["id"])
        if application["status"] in statuses:
            return application
        time.sleep(0.05)
    pytest.fail(
        "Worker did not reach "
        + repr(statuses)
        + ": "
        + repr({"status": application["status"], "reasons": (application.get("assessment") or {}).get("reasons")})
    )


def offered(client, letter="A"):
    application = command(client, prepared(client, letter), "submit")
    return wait_status(client, application, {"OFFER_READY"})


def accept(client, application):
    return command(
        client, application, "accept", confirmed=True, offer_id=application["offer"]["id"], offer_hash=application["offer"]["hash"]
    )


def test_term_application_persists_original_and_exact_schedule_offer(client):
    login(client)
    application = prepared(client)
    saved_input = deepcopy(application["input"])
    application = get_application(client, application["id"])
    assert application["input"] == saved_input
    application = command(client, application, "submit")
    application = wait_status(client, application, {"OFFER_READY"})
    assert application["assessment"]["risk"]["category"] == "A"
    assert application["versions"][0]["input"] == saved_input
    offer = application["offer"]
    assert offer["amount_kopecks"] == 200_000_000
    assert offer["term_months"] == 12
    assert offer["annual_rate_bps"] == 2400
    serialized = str(offer["calculation"])
    assert "18911919" in serialized
    assert "26943030" in serialized
    assert len(application["decisions"]) == len(application["offers"]) == 1


def test_larger_term_precedes_amount_reduction_and_client_can_reject(client):
    login(client, "fix-b")
    application = offered(client, "B")
    assert application["input"]["term_months"] == 12
    assert application["versions"][0]["input"]["amount_kopecks"] == 300_000_000
    assert application["offer"]["amount_kopecks"] == 300_000_000
    assert application["offer"]["term_months"] == 24
    assert application["decisions"][-1]["outcome"] == "APPROVE_CHANGED"
    assert "80671903" in str(application["offer"]["calculation"])
    application = command(
        client, application, "decline", confirmed=True, offer_id=application["offer"]["id"], offer_hash=application["offer"]["hash"]
    )
    assert application["status"] == "OFFER_REJECTED"
    assert application["contract_id"] is None
    assert workspace(client, "B")["account"]["balance_kopecks"] == 10_000_000


def test_submit_needs_two_consents_and_forecast_confirmation(client):
    login(client)
    application = create(client)
    error(command_response(client, application, "submit"), 422, "CONSENT_REQUIRED")
    application = command(client, application, "consent", purpose="CREDIT_HISTORY", confirmed=True)
    error(command_response(client, application, "submit"), 422, "CONSENT_REQUIRED")
    application = command(client, application, "consent", purpose="DATA_PROCESSING", confirmed=True)
    error(command_response(client, application, "submit"), 422, "FORECAST_CONFIRMATION_REQUIRED")
    assert get_application(client, application["id"])["status"] == "DRAFT"
    with SessionLocal() as db:
        assert not db.scalars(select(CreditRecord).where(CreditRecord.kind == "job")).all()


def test_idempotency_replay_precedes_revision_and_changed_payload_conflicts(client):
    login(client)
    key = str(uuid4())
    first = create(client, key=key)
    assert create(client, key=key) == first
    conflict = client.post(API + "/applications", json={"template_id": "FIX-D", "organization_id": "demo-org-d"}, headers=headers(key))
    error(conflict, 409, "IDEMPOTENCY_CONFLICT")
    application = prepared(client)
    key = str(uuid4())
    response = success(command_response(client, application, "submit", key=key))
    wait_status(client, response, {"OFFER_READY"})
    replay = success(command_response(client, application, "submit", key=key))
    assert replay == response
    current = get_application(client, application["id"])
    assert len(current["versions"]) == 1
    assert len(current["offers"]) == 1


def test_one_submitted_application_per_organization(client):
    login(client)
    first = offered(client)
    second = prepared(client)
    error(command_response(client, second, "submit"), 409, "ACTIVE_APPLICATION_EXISTS")
    first = command(client, first, "withdraw")
    assert first["status"] == "WITHDRAWN"
    second = command(client, second, "submit")
    assert wait_status(client, second, {"OFFER_READY"})["status"] == "OFFER_READY"


def test_stale_edit_does_not_overwrite_and_money_requires_integer_kopecks(client):
    login(client)
    initial = create(client)
    current = save(client, initial, {"amount_kopecks": 150_000_000})
    stale = client.put(
        API + "/applications/" + initial["id"],
        json={"expected_version": initial["revision"], "input": initial["input"]},
        headers=headers(revision=initial["revision"]),
    )
    error(stale, 409, "VERSION_CONFLICT")
    for amount in (1.5, True, -1):
        incoming = deepcopy(current["input"])
        incoming["amount_kopecks"] = amount
        response = client.put(
            API + "/applications/" + current["id"],
            json={"expected_version": current["revision"], "input": incoming},
            headers=headers(revision=current["revision"]),
        )
        error(response, 422, "INVALID_MONEY")
    assert get_application(client, current["id"])["input"]["amount_kopecks"] == 150_000_000


def test_client_cannot_override_registry_credit_history_or_source_results(client):
    login(client)
    application = create(client)
    application = save(
        client,
        application,
        {
            "organization": {"registration_date": "1900-01-01"},
            "active_overdue_days": 100,
            "source_checks": {"registry": "CLEAR"},
            "documents_verified": True,
        },
    )
    assert application["input"]["organization"]["registration_date"] != "1900-01-01"
    assert application["input"]["active_overdue_days"] == 0
    assert application["input"]["source_checks"]["registry"] == "CLEAR"


def test_wrong_offer_hash_and_bundle_hash_cannot_create_signing(client):
    login(client)
    application = offered(client)
    error(
        command_response(client, application, "accept", confirmed=True, offer_id=application["offer"]["id"], offer_hash="f" * 64),
        409,
        "OFFER_STALE",
    )
    application = accept(client, application)
    bundle = success(client.get(API + "/applications/" + application["id"] + "/bundle"))
    assert bundle["offer_id"] == application["offer"]["id"]
    assert bundle["acceptance_id"] == application["acceptance"]["id"]
    assert len(bundle["documents"]) == 4
    assert all(document["hash"] and document["html"] for document in bundle["documents"])
    error(
        command_response(
            client,
            application,
            "sign",
            confirmed=True,
            offer_id=application["offer"]["id"],
            offer_hash=application["offer"]["hash"],
            bundle_hash="f" * 64,
        ),
        409,
        "BUNDLE_STALE",
    )
    assert get_application(client, application["id"])["signing"] is None


def test_tenant_files_and_workspace_are_hidden(client):
    login(client)
    application = create(client)
    document_id = application["documents"][0]["id"]
    login(client, "fix-b")
    error(client.get(API + "/applications/" + application["id"]), 404)
    error(client.get(API + "/workspace", params={"organization_id": "demo-org-a"}), 404)
    # Endpoint is intentionally authorized by application as well as file ID.
    error(client.get(API + "/documents/" + document_id + "/content"), 404)


def test_preparer_can_edit_but_cannot_submit_accept_or_sign(client):
    login(client, "prepare")
    application = create(client, "B")
    application = save(client, application, {"financials_confirmed": True})
    for action in ("submit", "consent", "accept", "sign"):
        error(command_response(client, application, action, confirmed=True, purpose="CREDIT_HISTORY"), 403, "AUTHORITY_REQUIRED")
    assert get_application(client, application["id"])["status"] == "DRAFT"


def test_demo_admin_has_no_economic_or_document_access(client):
    login(client)
    application = create(client)
    login(client, "admin")
    state = success(client.get(API + "/workspace"))
    assert state["roles"] == ["DEMO_ADMIN"]
    assert state["account"] is None and not state["contracts"]
    assert all("input" not in row and "documents" not in row for row in state["applications"])
    error(command_response(client, application, "decide", outcome="APPROVE", reason="Should never approve"), 403)
    error(client.get(API + "/applications/" + application["id"]), 403)


@pytest.mark.parametrize(("letter", "expected"), [("C", "MANUAL_REVIEW"), ("F", "NOT_ELIGIBLE"), ("G", "MANUAL_REVIEW")])
def test_seasonality_young_business_and_negative_flow_are_distinct(client, letter, expected):
    login(client, "fix-" + letter.lower())
    application = command(client, prepared(client, letter), "submit")
    application = wait_status(client, application, {expected})
    assert application["offer"] is None and application["contract_id"] is None
    if letter == "G":
        assert application["assessment"]["risk"]["category"] == "C"
        assert not application["decisions"]
    if letter == "F":
        assert application["status"] != "DECLINED"


def test_claim_is_exclusive_and_manual_approve_cannot_bypass_cashflow(client):
    login(client, "fix-c")
    application = command(client, prepared(client, "C"), "submit")
    application = wait_status(client, application, {"MANUAL_REVIEW"})
    login(client, "bank")
    error(
        command_response(
            client,
            application,
            "decide",
            outcome="APPROVE",
            reason="Need an assigned analyst",
            public_explanation="Проверка полномочий аналитика",
        ),
        403,
        "ASSIGNMENT_REQUIRED",
    )
    application = command(client, application, "claim")
    assigned_id = application["assigned_analyst_id"]
    login(client, "bank2")
    error(command_response(client, application, "claim"), 409, "ALREADY_ASSIGNED")
    error(
        command_response(
            client, application, "request_info", question="Подтвердите денежный резерв на первую дату", fields=["initial_cash_kopecks"]
        ),
        403,
        "ASSIGNMENT_REQUIRED",
    )
    login(client, "bank")
    error(
        command_response(
            client,
            application,
            "decide",
            outcome="APPROVE",
            reason="Попытка обойти недостаток денежного потока",
            public_explanation="Проверка обязательных финансовых ограничений",
        ),
        409,
        "MANDATORY_GATES",
    )
    current = get_application(client, application["id"])
    assert current["assigned_analyst_id"] == assigned_id
    assert current["status"] == "MANUAL_REVIEW" and current["offer"] is None


def test_source_timeout_is_waiting_not_economic_rejection_and_recovers(client):
    login(client, "fix-i")
    application = command(client, prepared(client, "I"), "submit")
    waiting = wait_status(client, application, {"WAITING_SOURCE"})
    assert waiting["assessment"] is None and not waiting["decisions"]
    assert any(check["technical_code"] == "TIMEOUT" and check["result"] is None for check in waiting["checks"])
    completed = wait_status(client, application, {"OFFER_READY"})
    history = [check for check in completed["checks"] if check["source"] == "credit_history"]
    assert [check["status"] for check in history] == ["FAILED_RETRYABLE", "FAILED_RETRYABLE", "SUCCEEDED"]
    assert len(completed["versions"]) == 1
    assert len(completed["decisions"]) == 1


def test_missing_statement_period_creates_precise_question_and_keeps_original(client):
    login(client, "fix-e")
    application = command(client, prepared(client, "E"), "submit")
    application = wait_status(client, application, {"NEEDS_INFO"})
    statements = [doc for doc in application["documents"] if doc["kind"] == "statement"]
    assert len(statements) == 1 and len(statements[0]["periods"]) == 5
    question = application["clarifications"][0]
    assert question["fields"] == ["documents.statement.last_period"]
    assert question["status"] == "OPEN" and len(question["question"]) > 20
    assert len(application["versions"]) == 1
    assert not application["decisions"]


def contract_command(client, contract, action, key=None, **extra):
    return client.post(
        API + "/contracts/" + contract["id"] + "/commands",
        json={"action": action, "expected_version": contract["revision"], **extra},
        headers=headers(key, contract["revision"]),
    )


def active_contract(client, letter="A"):
    app = accept(client, offered(client, letter))
    app = command(client, app, "sign", confirmed=True, bundle_hash=app["bundle"]["hash"])
    app = wait_status(client, app, {"CONTRACT_ACTIVE"})
    return success(client.get(API + "/contracts/" + app["contract_id"]))


def advance(client, days, back="good"):
    login(client, "admin")
    state = success(client.get(API + "/workspace"))
    result = success(
        client.post(
            API + "/admin/commands",
            json={"action": "advance", "expected_version": state["clock"]["revision"], "days": days},
            headers=headers(),
        )
    )
    login(client, back)
    return result


def draw_cycle(on, amount=60000000, days=30):
    from datetime import date, timedelta

    def day(n):
        return (date.fromisoformat(on) + timedelta(days=n)).isoformat()

    token = str(uuid4())
    total = amount * 13 // 10
    first = min(20000000, total)
    return {
        "confirmed": True,
        "initial_cash_kopecks": 10000000,
        "events": [
            {"id": token + "target", "date": on, "kind": "TARGET_PAYMENT", "amount_kopecks": amount},
            {"id": token + "operating", "date": day(1), "kind": "OPERATING_PAYMENT", "amount_kopecks": 5000000},
            {"id": token + "buyer10", "date": day(10), "kind": "CUSTOMER_RECEIPT", "amount_kopecks": first},
            {"id": token + "buyerEnd", "date": day(days), "kind": "CUSTOMER_RECEIPT", "amount_kopecks": total - first},
        ],
    }


def wait_contract(client, c, predicate):
    for _ in range(100):
        c = success(client.get(API + "/contracts/" + c["id"]))
        if predicate(c):
            return c
        time.sleep(0.05)
    pytest.fail("Contract worker failed: " + str(c.get("tranches")))


def test_term_executes_once_and_due_payment_allocates_exactly(client):
    login(client)
    c = active_contract(client)
    assert c["principal_outstanding_kopecks"] == 200000000
    assert len(c["schedule"]) == 12
    assert sum(r["principal_kopecks"] for r in c["schedule"]) == 200000000
    assert c["account"]["balance_kopecks"] == 10000000
    assert len([o for o in c["operations"] if o["kind"] == "LOAN_DISBURSEMENT"]) == 1
    error(contract_command(client, c, "pay_preview"), 422, "PAYMENT_NOT_DUE")
    advance(client, 31)
    c = success(client.get(API + "/contracts/" + c["id"]))
    assert c["schedule"][0]["status"] == "DUE"
    q = success(contract_command(client, c, "pay_preview"))["quote"]
    assert q["amount_kopecks"] == 18911919
    assert q["interest_kopecks"] == 4000000
    key = str(uuid4())
    response = contract_command(client, c, "pay", key=key, quote_id=q["id"], confirmed=True)
    paid = success(response)
    assert paid["principal_outstanding_kopecks"] == 185088081
    assert paid["schedule"][0]["status"] == "PAID"
    replay = success(contract_command(client, c, "pay", key=key, quote_id=q["id"], confirmed=True))
    assert replay == paid
    error(contract_command(client, paid, "pay", quote_id=q["id"], confirmed=True), 409, "QUOTE_STALE")


def test_line_zero_activation_partial_repayment_and_restoration(client):
    login(client, "fix-d")
    c = active_contract(client, "D")
    assert c["facility"]["available_kopecks"] == 100000000
    assert c["facility"]["drawn_kopecks"] == c["facility"]["reserved_kopecks"] == 0
    assert not c["operations"]
    on = workspace(client, "D")["clock"]["date"]
    q = success(contract_command(client, c, "draw_preview", amount_kopecks=60000000, days=30, cycle_plan=draw_cycle(on)))["quote"]
    reserved = success(contract_command(client, c, "draw", quote_id=q["id"], confirmed=True))
    assert reserved["facility"]["reserved_kopecks"] == 60000000
    assert reserved["facility"]["available_kopecks"] == 40000000
    c = wait_contract(client, reserved, lambda x: x["facility"]["drawn_kopecks"] == 60000000)
    assert c["facility"]["reserved_kopecks"] == 0
    assert c["facility"]["available_kopecks"] == 40000000
    advance(client, 10, "fix-d")
    c = success(client.get(API + "/contracts/" + c["id"]))
    tid = c["tranches"][0]["id"]
    assert c["tranches"][0]["interest_kopecks"] == 394521
    q = success(contract_command(client, c, "repay_preview", tranche_id=tid, amount_kopecks=10394521))["quote"]
    c = success(contract_command(client, c, "repay", quote_id=q["id"], confirmed=True))
    assert c["facility"]["drawn_kopecks"] == 50000000
    assert c["facility"]["available_kopecks"] == 50000000
    advance(client, 20, "fix-d")
    c = success(client.get(API + "/contracts/" + c["id"]))
    q = success(contract_command(client, c, "repay_preview", tranche_id=tid, full=True))["quote"]
    assert q["amount_kopecks"] == 50657534
    c = success(contract_command(client, c, "repay", quote_id=q["id"], confirmed=True))
    assert c["facility"]["available_kopecks"] == 100000000
    assert c["facility"]["drawn_kopecks"] == 0
    assert c["tranches"][0]["status"] == "REPAID"
    assert sum(o.get("interest_kopecks", 0) for o in c["operations"] if o["kind"] == "REPAYMENT") == 1052055
    assert c["status"] == "ACTIVE"  # Empty line remains usable until its expiry.


def test_interest_only_payment_does_not_restore_limit_and_old_quote_stales(client):
    login(client, "fix-d")
    c = active_contract(client, "D")
    on = workspace(client, "D")["clock"]["date"]
    q = success(contract_command(client, c, "draw_preview", amount_kopecks=60000000, days=30, cycle_plan=draw_cycle(on)))["quote"]
    c = success(contract_command(client, c, "draw", quote_id=q["id"], confirmed=True))
    c = wait_contract(client, c, lambda x: x["facility"]["drawn_kopecks"] == 60000000)
    advance(client, 10, "fix-d")
    c = success(client.get(API + "/contracts/" + c["id"]))
    tid = c["tranches"][0]["id"]
    q = success(contract_command(client, c, "repay_preview", tranche_id=tid, amount_kopecks=394521))["quote"]
    c = success(contract_command(client, c, "repay", quote_id=q["id"], confirmed=True))
    assert c["facility"]["drawn_kopecks"] == 60000000
    assert c["facility"]["available_kopecks"] == 40000000
    error(
        contract_command(
            client,
            c,
            "draw_preview",
            amount_kopecks=5000000,
            days=30,
            cycle_plan=draw_cycle(workspace(client, "D")["clock"]["date"], 5000000),
        ),
        422,
        "CHECKS_STALE",
    )
    c = success(contract_command(client, c, "refresh_checks"))
    assert c["tranches"][0]["interest_kopecks"] == 0


def test_offer_calendar_change_requires_new_acceptance(client):
    login(client)
    app = offered(client)
    old_offer = app["offer"]["id"]
    advance(client, 1)
    app = command(client, app, "accept", confirmed=True, offer_id=old_offer, offer_hash=app["offer"]["hash"])
    assert app["status"] == "PROCESSING"
    assert app["acceptance"] is None
    app = wait_status(client, app, {"OFFER_READY"})
    assert app["offer"]["id"] != old_offer
    assert app["offer"]["planned_on"] == "2026-10-26"
    assert app["offers"][0]["status"] == "REVOKED"
    error(
        command_response(client, app, "accept", confirmed=True, offer_id=old_offer, offer_hash=app["offers"][0]["hash"]), 409, "OFFER_STALE"
    )


def test_unknown_pdf_requires_substantive_analyst_review(client):
    login(client)
    app = prepared(client)
    response = client.post(
        API + "/applications/" + app["id"] + "/documents",
        data={"kind": "purpose", "expected_version": str(app["revision"])},
        files={"file": ("purpose.pdf", b"%PDF-1.7\nsynthetic new file", "application/pdf")},
        headers=headers(),
    )
    app = success(response)
    assert app["documents"][-1]["status"] == "UPLOADED"
    app = command(client, app, "submit")
    app = wait_status(client, app, {"MANUAL_REVIEW"})
    assert any(r["code"] == "DOCUMENT_REVIEW" for r in app["assessment"]["reasons"])
    assert not app["offer"]


def test_missing_statement_replacement_creates_new_document_and_version(client):
    login(client, "fix-e")
    app = command(client, prepared(client, "E"), "submit")
    app = wait_status(client, app, {"NEEDS_INFO"})
    original = next(d for d in app["documents"] if d["kind"] == "statement")
    template = client.get(API + "/applications/" + app["id"] + "/document-template")
    assert template.status_code == 200
    app = success(
        client.post(
            API + "/applications/" + app["id"] + "/documents",
            data={"kind": "statement", "expected_version": str(app["revision"])},
            files={"file": ("complete.csv", template.content, "text/csv")},
            headers=headers(),
        )
    )
    assert app["documents"][-1]["version"] == 2
    assert app["documents"][0]["status"] == "SUPERSEDED"
    app = command(client, app, "respond", input=app["input"], message="Дополнен последний месяц выписки", confirmed=True)
    app = wait_status(client, app, {"OFFER_READY"})
    assert len(app["versions"]) == 2
    assert len([d for d in app["documents"] if d["kind"] == "statement"]) == 2
    assert client.get(API + "/documents/" + original["id"] + "/content").status_code == 200


def test_existing_internal_loan_cannot_be_removed_from_new_assessment(client):
    login(client)
    contract = active_contract(client)
    app = prepared(client)
    value = deepcopy(app["input"])
    for p in value["forecast"]:
        p["old_debt_service_kopecks"] = 0
    app = save(client, app, value)
    app = command(client, app, "submit")
    app = wait_status(client, app, {"MANUAL_REVIEW", "OFFER_READY"})
    snapshot = app["assessment"]["input_snapshot"]
    assert snapshot["forecast"][0]["old_debt_service_kopecks"] == 10000000 + 18911919
    assert len([o for o in snapshot["obligations"] if o["canonical_id"] == contract["id"]]) == 1
    # The unchanged initial structure cannot be approved a second time.
    assert not app["assessment"]["original"]["passed"]


def test_concurrent_reservations_and_replay_never_exceed_limit(client):
    from concurrent.futures import ThreadPoolExecutor
    from fastapi.testclient import TestClient
    from app.main import app as api_app

    login(client, "fix-k")
    c = active_contract(client, "K")
    on = workspace(client, "K")["clock"]["date"]
    quotes = [
        success(contract_command(client, c, "draw_preview", amount_kopecks=80000000, days=30, cycle_plan=draw_cycle(on, 80000000)))["quote"]
        for _ in range(2)
    ]

    def send(q):
        rival = TestClient(api_app)
        try:
            rival.cookies.update(client.cookies)
            rival.headers["X-CSRF-Token"] = client.headers["X-CSRF-Token"]
            return contract_command(rival, c, "draw", quote_id=q["id"], confirmed=True).status_code
        finally:
            rival.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(send, quotes))
    assert sorted(statuses) == [200, 409]
    c = wait_contract(client, c, lambda x: x["facility"]["drawn_kopecks"] == 80000000)
    assert c["facility"]["drawn_kopecks"] + c["facility"]["reserved_kopecks"] <= 100000000
    assert len([o for o in c["operations"] if o["kind"] == "TRANCHE_DISBURSEMENT"]) == 1
    error(
        contract_command(client, c, "draw_preview", amount_kopecks=80000000, days=30, cycle_plan=draw_cycle(on, 80000000)),
        422,
        "LIMIT_INSUFFICIENT",
    )


def test_clock_blocks_reserved_money_and_signed_withdraw_cancels_job(client, monkeypatch):
    from app import v2

    login(client)
    app = accept(client, offered(client))
    monkeypatch.setattr(v2, "process_due_jobs", lambda: 0)
    app = command(client, app, "sign", confirmed=True, bundle_hash=app["bundle"]["hash"])
    assert app["status"] == "SIGNED"
    login(client, "admin")
    state = success(client.get(API + "/workspace"))
    error(
        client.post(
            API + "/admin/commands",
            json={"action": "advance", "expected_version": state["clock"]["revision"], "days": 1},
            headers=headers(),
        ),
        409,
        "PENDING_FINANCIAL_EXECUTION",
    )
    login(client)
    app = command(client, app, "withdraw")
    assert app["status"] == "WITHDRAWN"
    with SessionLocal() as db:
        assert db.get(CreditRecord, app["execution_job_id"]).data["status"] == "CANCELLED"
        assert not db.scalars(select(CreditRecord).where(CreditRecord.kind == "contract")).all()


def test_all_term_rows_close_contract_with_no_penny_debt(client):
    from datetime import date

    login(client)
    c = active_contract(client)
    for index in range(12):
        today = workspace(client)["clock"]["date"]
        due = c["schedule"][index]["due_date"]
        days = (date.fromisoformat(due) - date.fromisoformat(today)).days
        advance(client, days)
        c = success(client.get(API + "/contracts/" + c["id"]))
        q = success(contract_command(client, c, "pay_preview"))["quote"]
        c = success(contract_command(client, c, "pay", quote_id=q["id"], confirmed=True))
    assert c["status"] == "CLOSED"
    assert c["principal_outstanding_kopecks"] == 0
    assert all(r["status"] == "PAID" for r in c["schedule"])
    assert sum(o["amount_kopecks"] for o in c["operations"] if o["kind"] == "REPAYMENT") == 226943030
    a = get_application(client, c["application_id"])
    assert a["status"] == "CONTRACT_ACTIVE"
    assert len([o for o in c["operations"] if o["kind"] == "LOAN_DISBURSEMENT"]) == 1


def test_line_overdue_blocks_draw_but_never_erases_principal(client):
    login(client, "fix-l")
    c = active_contract(client, "L")
    on = workspace(client, "L")["clock"]["date"]
    q = success(contract_command(client, c, "draw_preview", amount_kopecks=60000000, days=30, cycle_plan=draw_cycle(on)))["quote"]
    c = success(contract_command(client, c, "draw", quote_id=q["id"], confirmed=True))
    c = wait_contract(client, c, lambda x: x["facility"]["drawn_kopecks"] == 60000000)
    advance(client, 31, "fix-l")
    c = success(client.get(API + "/contracts/" + c["id"]))
    assert c["facility"]["status"] == "BLOCKED"
    assert c["tranches"][0]["status"] == "OVERDUE"
    assert c["facility"]["drawn_kopecks"] == 60000000
    error(
        contract_command(
            client,
            c,
            "draw_preview",
            amount_kopecks=5000000,
            days=30,
            cycle_plan=draw_cycle(workspace(client, "L")["clock"]["date"], 5000000),
        ),
        422,
        "FACILITY_NOT_ACTIVE",
    )
    tid = c["tranches"][0]["id"]
    q = success(contract_command(client, c, "repay_preview", tranche_id=tid, full=True))["quote"]
    c = success(contract_command(client, c, "repay", quote_id=q["id"], confirmed=True))
    assert c["facility"]["drawn_kopecks"] == 0
    for days in [31] * 10 + [25]:
        advance(client, days, "fix-l")
    c = success(client.get(API + "/contracts/" + c["id"]))
    assert c["facility"]["status"] == "CLOSED"
    assert c["status"] == "CLOSED"
    assert c["tranches"][0]["status"] == "REPAID"


def test_session_inactivity_uses_real_time_not_demo_clock(client):
    from datetime import timedelta
    import hashlib
    from app.models import Session, now

    login(client)
    token = client.cookies.get("credit_session")
    with SessionLocal() as db:
        session = db.get(Session, hashlib.sha256(token.encode()).hexdigest())
        session.last_seen_at = now() - timedelta(minutes=31)
        db.commit()
    error(client.get(API + "/workspace"), 401)


def test_forecast_duplicate_period_requires_clarification_before_risk(client):
    login(client)
    app = prepared(client)
    incoming = deepcopy(app["input"])
    incoming["forecast"][1]["period"] = incoming["forecast"][0]["period"]
    app = save(client, app, incoming)
    app = command(client, app, "submit")
    app = wait_status(client, app, {"NEEDS_INFO"})
    assert app["assessment"]["risk"]["category"] == "NOT_CALCULATED"
    assert any(r.get("field") == "forecast" for r in app["assessment"]["reasons"])


def test_economic_refusal_is_an_analyst_decision_not_risk_category(client):
    login(client, "fix-g")
    a = command(client, prepared(client, "G"), "submit")
    a = wait_status(client, a, {"MANUAL_REVIEW"})
    assert a["assessment"]["risk"]["category"] == "C" and not a["decisions"]
    login(client, "bank")
    a = command(client, a, "claim")
    a = command(
        client,
        a,
        "decide",
        outcome="DECLINE",
        reason="Подтверждены отрицательные CFADS шести месяцев и первых трех прогнозных интервалов",
        public_explanation="Подтвержденный поток не покрывает расходы и возврат кредита; требуется устойчивый источник возврата",
    )
    assert a["status"] == "DECLINED"
    assert a["decisions"][-1]["outcome"] == "DECLINE"
    assert a["decisions"][-1]["actor"]
    assert not a["contract_id"]


def test_classification_excludes_transfers_credit_and_deposit_without_erasing_file(client):
    login(client, "fix-h")
    a = command(client, prepared(client, "H"), "submit")
    a = wait_status(client, a, {"NEEDS_INFO"})
    document = next(d for d in a["documents"] if d["kind"] == "statement")
    original = client.get(API + "/documents/" + document["id"] + "/content").content
    assert b"500000000" in original
    assert a["assessment"]["reasons"][0]["code"] == "RECONCILIATION_REQUIRED"
    a = command(
        client,
        a,
        "respond",
        input=a["input"],
        message="Подтверждаю: 3 млн собственных переводов, 2 млн кредита, 1 млн возврата депозита; покупатели 24 млн",
        confirm_classification=True,
    )
    a = wait_status(client, a, {"OFFER_READY"})
    assert a["statement_classification"]["customer_receipts_kopecks"] == 2400000000
    assert sum(p["customer_receipts_kopecks"] for p in a["assessment"]["input_snapshot"]["history"]) == 2400000000
    assert client.get(API + "/documents/" + document["id"] + "/content").content == original
    assert len(a["versions"]) == 2


def test_expired_offer_and_expired_authority_block_new_actions(client):
    login(client, "fix-j")
    a = offered(client, "J")
    advance(client, 8, "fix-j")
    stale = get_application(client, a["id"])
    assert stale["status"] == "OFFER_EXPIRED"
    error(
        command_response(client, stale, "accept", offer_id=a["offer"]["id"], offer_hash=a["offer"]["hash"], confirmed=True),
        409,
        "INVALID_STATE",
    )
    advance(client, 1, "fix-j")
    new = create(client, "J")
    error(command_response(client, new, "consent", purpose="CREDIT_HISTORY", confirmed=True), 403, "AUTHORITY_REQUIRED")


def test_client_receives_public_explanation_but_never_internal_bank_reason(client):
    import json

    login(client)
    a = prepared(client)
    a = save(client, a, {"concentration_bps": 6000})
    a = command(client, a, "submit")
    a = wait_status(client, a, {"MANUAL_REVIEW"})
    login(client, "bank")
    a = command(client, a, "claim")
    a = command(
        client,
        a,
        "decide",
        outcome="APPROVE",
        reason="INTERNAL_ONLY: проверена концентрация и подтвержден запас платежного потока",
        public_explanation="Подтвержденный календарь покрывает все платежи; условия исходного запроса доступны",
    )
    assert "INTERNAL_ONLY" in json.dumps(a, ensure_ascii=False)
    login(client)
    visible = get_application(client, a["id"])
    assert "INTERNAL_ONLY" not in json.dumps(visible, ensure_ascii=False)
    assert "internal_reason" not in json.dumps(visible, ensure_ascii=False)
    assert visible["decisions"][-1]["public_explanation"]
    assert "INTERNAL_ONLY" not in json.dumps(workspace(client), ensure_ascii=False)
