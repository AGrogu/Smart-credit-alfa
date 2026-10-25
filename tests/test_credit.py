from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app.main import app
from app.db import SessionLocal
from app.models import Application, Company, User, Document
from app.lending import calculate, score, decide
from app.security import password_hash, verify_password
from conftest import login

PAYLOAD = {
    "product_id": "working",
    "amount": "500000.00",
    "months": 12,
    "purpose": "Пополнение запасов",
    "revenue": "1000000.00",
    "expenses": "600000.00",
    "monthly_debt": "20000.00",
}


def new_application(client, document=True, **overrides):
    response = client.post("/api/applications", json=PAYLOAD | overrides, headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 201, response.text
    data = response.json()
    if document:
        upload = client.post(
            f"/api/applications/{data['id']}/documents",
            files={"file": ("report.txt", "Синтетическая выручка: 1000000".encode(), "text/plain")},
        )
        assert upload.status_code == 201, upload.text
    return client.get(f"/api/applications/{data['id']}").json()


def act(client, data, action, **extra):
    response = client.post(
        f"/api/applications/{data['id']}/actions",
        json={"action": action, "version": data["version"], **extra},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_money_exact_annuity():
    calc = calculate("working", Decimal("500000"), 12)
    assert calc == {"payment": "45840.00", "total": "550080.00", "overpayment": "50080.00", "annual_rate": "18.00"}
    assert Decimal(calc["total"]) - Decimal("500000") == Decimal(calc["overpayment"])


@pytest.mark.parametrize(
    "product,amount,months", [("working", "99999", 12), ("working", "5000001", 12), ("working", "100000", 2), ("equipment", "250000", 61)]
)
def test_term_limits(product, amount, months):
    with pytest.raises(HTTPException):
        calculate(product, Decimal(amount), months)


def test_score_product_policy_and_immutable_bki():
    data = SimpleNamespace(
        company_snapshot={"registration_months": 15},
        revenue=Decimal("1000000"),
        expenses=Decimal("600000"),
        monthly_debt=Decimal("20000"),
        amount=Decimal("500000"),
        months=12,
        product_id="working",
        calculation=calculate("working", Decimal("500000"), 12),
    )
    risk = score(data, {})
    assert risk["score"] == 10
    assert decide(data, {}, risk)[0] == "approve"
    data.product_id = "equipment"
    assert decide(data, {}, risk)[0] == "manual_review"
    assert decide(data, {"overdue": True}, score(data, {"overdue": True}))[0] == "reject"


@pytest.mark.parametrize(
    "account,expected", [("good", "approved"), ("risk", "rejected"), ("review", "manual_review"), ("retry", "check_failed")]
)
def test_deterministic_branches(client, account, expected):
    login(client, account)
    data = act(client, new_application(client), "submit")
    assert data["status"] == expected
    assert [x["status"] for x in data["history"]][:3] == ["draft", "submitted", "checking"]
    if expected == "check_failed":
        assert act(client, data, "retry")["status"] == "approved"


def test_incomplete_response_and_history(owner):
    data = act(owner, new_application(owner, revenue=None), "submit")
    assert data["status"] == "needs_info"
    data = act(owner, data, "respond", message="Прикладываю уточнённую выручку", revenue="1000000.00")
    assert data["status"] == "approved"
    assert data["responses"][0]["message"] == "Прикладываю уточнённую выручку"
    assert data["history"][1]["financial_snapshot"]["revenue"] is None


def test_missing_document_request(owner):
    data = act(owner, new_application(owner, document=False), "submit")
    assert data["status"] == "needs_info"
    upload = owner.post(f"/api/applications/{data['id']}/documents", files={"file": ("report.txt", b"Synthetic", "text/plain")})
    assert upload.status_code == 201
    data = owner.get(f"/api/applications/{data['id']}").json()
    assert act(owner, data, "respond", message="Документ добавлен")["status"] == "approved"


def test_offer_accept_sign_disburse_replay(owner):
    data = act(owner, new_application(owner, amount="4500000"), "submit")
    assert data["status"] == "alternative_offered"
    offer = data["offer"]
    assert Decimal(offer["amount"]) < Decimal(data["amount"])
    data = act(owner, data, "accept")
    data = act(owner, data, "sign")
    key = str(uuid4())
    body = {"action": "disburse", "version": data["version"]}
    url = f"/api/applications/{data['id']}/actions"
    first = owner.post(url, json=body, headers={"Idempotency-Key": key})
    replay = owner.post(url, json=body, headers={"Idempotency-Key": key})
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    data = first.json()
    assert data["status"] == "disbursed"
    assert data["disbursement"]["amount"] == offer["amount"]
    assert sum(h["status"] == "disbursed" for h in data["history"]) == 1
    assert (
        owner.post(url, json={"action": "disburse", "version": data["version"]}, headers={"Idempotency-Key": str(uuid4())}).status_code
        == 409
    )


@pytest.mark.parametrize("action,status", [("decline", "declined"), ("cancel", "cancelled")])
def test_client_end_states(owner, action, status):
    data = act(owner, new_application(owner), "submit")
    assert act(owner, data, action)["status"] == status


@pytest.mark.parametrize("decision,status", [("approve", "approved"), ("reject", "rejected"), ("offer", "alternative_offered")])
def test_manual_decisions_and_private_notes(client, decision, status):
    login(client, "review")
    data = act(client, new_application(client), "submit")
    login(client, "bank")
    bank_data = client.get(f"/api/applications/{data['id']}").json()
    assert bank_data["risk"]["version"] == "demo-v1"
    assert bank_data["recommendation"]["type"] == "manual_review"
    assert bank_data["decision"] is None
    url = f"/api/applications/{data['id']}"
    assert client.post(url + "/notes", json={"message": "СЕКРЕТНАЯ СЛУЖЕБНАЯ ЗАМЕТКА"}).status_code == 201
    missing = client.post(
        url + "/actions", json={"action": decision, "version": data["version"]}, headers={"Idempotency-Key": str(uuid4())}
    )
    assert missing.status_code == 422
    extra = {"amount": "300000", "months": 18} if decision == "offer" else {}
    data = act(client, data, decision, reason="Проверены документы и денежный поток", **extra)
    assert data["status"] == status and data["decision"]["source"] == "manual"
    assert data["recommendation"]["type"] == "manual_review"
    login(client, "review")
    visible = client.get(url).json()
    assert not any(k in visible for k in ["risk", "checks", "notes", "recommendation", "evaluation_history"])
    assert "СЕКРЕТНАЯ" not in str(visible)
    assert client.post(url + "/notes", json={"message": "Не сотрудник"}).status_code == 403


def test_bank_request_and_client_response(client):
    login(client, "review")
    data = act(client, new_application(client), "submit")
    login(client, "bank")
    data = act(client, data, "request_info", reason="Нужно подтверждение", message="Уточните структуру выручки")
    assert data["status"] == "needs_info"
    login(client, "review")
    data = act(client, data, "respond", message="Доход от розничных продаж")
    assert data["status"] == "manual_review"
    login(client, "bank")
    assert act(client, data, "approve", reason="Дополнительные сведения приняты")["status"] == "approved"


def test_isolation_role_and_document_idor(owner):
    data = new_application(owner)
    doc = data["documents"][0]["id"]
    original = owner.get(f"/api/documents/{doc}")
    assert original.status_code == 200
    assert original.headers["content-disposition"].startswith("attachment;")
    assert original.headers["x-content-type-options"] == "nosniff"
    login(owner, "risk")
    assert owner.get(f"/api/applications/{data['id']}").status_code == 404
    assert owner.get(f"/api/documents/{doc}").status_code == 404
    assert owner.post(f"/api/applications/{data['id']}/documents", files={"file": ("evil.txt", b"x", "text/plain")}).status_code == 404
    assert owner.get("/api/bank/summary").status_code == 403
    assert owner.get("/api/bank/audit").status_code == 403
    assert data["id"] not in [a["id"] for a in owner.get("/api/applications").json()]


@pytest.mark.parametrize(
    "filename,content,status",
    [
        ("report.html", b"<script>x</script>", 422),
        ("fake.pdf", b"not a PDF", 422),
        ("empty.txt", b"", 422),
        ("binary.txt", b"\x00\xff", 422),
        ("huge.txt", b"x" * (5 * 1024 * 1024 + 1), 413),
    ],
)
def test_upload_validation(owner, filename, content, status):
    data = new_application(owner, document=False)
    response = owner.post(f"/api/applications/{data['id']}/documents", files={"file": (filename, content, "application/octet-stream")})
    assert response.status_code == status, response.text


@pytest.mark.parametrize(
    "extra",
    [
        {"amount": "-1"},
        {"amount": "100000.001"},
        {"months": 0},
        {"product_id": "unknown"},
        {"scenario": "good"},
        {"company_id": "other"},
        {"revenue": "NaN"},
    ],
)
def test_server_validation(owner, extra):
    response = owner.post("/api/applications", json=PAYLOAD | extra, headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 422, response.text


def test_csrf_origin_session_and_password(owner):
    assert owner.get("/api/auth/me").status_code == 200
    csrf = owner.headers.pop("X-CSRF-Token")
    assert owner.post("/api/calculate", json={"product_id": "working", "amount": "500000", "months": 12}).status_code == 403
    owner.headers["X-CSRF-Token"] = csrf
    assert owner.post("/api/auth/logout", headers={"Origin": "https://attacker.invalid"}).status_code == 403
    assert owner.post("/api/auth/logout").status_code == 200
    assert owner.get("/api/auth/me").status_code == 401
    encoded = password_hash("password")
    assert encoded != "password" and verify_password("password", encoded) and not verify_password("wrong", encoded)


def test_profile_cannot_edit_simulator_and_snapshot_frozen(owner):
    data = act(owner, new_application(owner), "submit")
    company = owner.get("/api/company").json()
    edited = {k: company[k] for k in ("name", "industry", "revenue", "expenses", "monthly_debt")}
    edited["name"] = "Новое синтетическое название"
    assert owner.put("/api/company", json=edited | {"fixture": {"overdue": False}}).status_code == 422
    assert owner.put("/api/company", json=edited).status_code == 200
    assert owner.get(f"/api/applications/{data['id']}").json()["company_snapshot"] == data["company_snapshot"]


def test_duplicate_create_and_key_reuse(owner):
    key = str(uuid4())
    headers = {"Idempotency-Key": key}
    first = owner.post("/api/applications", json=PAYLOAD, headers=headers)
    second = owner.post("/api/applications", json=PAYLOAD, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert owner.post("/api/applications", json=PAYLOAD | {"amount": "600000"}, headers=headers).status_code == 409


def test_competing_submit(owner):
    data = new_application(owner)
    url = f"/api/applications/{data['id']}/actions"

    def request(_):
        rival = TestClient(app)
        try:
            rival.cookies.update(owner.cookies)
            return rival.post(
                url,
                json={"action": "submit", "version": data["version"]},
                headers={"X-CSRF-Token": owner.headers["X-CSRF-Token"], "Idempotency-Key": str(uuid4())},
            ).status_code
        finally:
            rival.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, range(2)))
    assert sorted(results) == [200, 409]
    saved = owner.get(f"/api/applications/{data['id']}").json()
    assert sum(h["status"] == "submitted" for h in saved["history"]) == 1


def test_competing_decisions(client):
    login(client, "review")
    data = act(client, new_application(client), "submit")
    login(client, "bank")

    def request(command):
        rival = TestClient(app)
        try:
            rival.cookies.update(client.cookies)
            return rival.post(
                f"/api/applications/{data['id']}/actions",
                json={"action": command, "version": data["version"], "reason": "Проверка конкурентного решения"},
                headers={"X-CSRF-Token": client.headers["X-CSRF-Token"], "Idempotency-Key": str(uuid4())},
            ).status_code
        finally:
            rival.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, ["approve", "reject"]))
    assert sorted(results) == [200, 409]


def test_restart_seed_persistence(owner):
    data = new_application(owner)
    doc = data["documents"][0]["id"]
    with SessionLocal() as db:
        before = db.scalar(select(func.count()).select_from(User))
    with TestClient(app) as restarted:
        restarted.cookies.update(owner.cookies)
        saved = restarted.get(f"/api/applications/{data['id']}")
        assert saved.status_code == 200 and saved.json() == data
        assert restarted.get(f"/api/documents/{doc}").status_code == 200
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(User)) == before == 22
        assert db.scalar(select(func.count()).select_from(Company)) == 6
        assert db.get(Application, data["id"]) is not None
        assert db.get(Document, doc) is not None


def test_status_guard(owner):
    data = new_application(owner)
    for action in ("accept", "sign", "disburse", "retry", "respond"):
        response = owner.post(
            f"/api/applications/{data['id']}/actions",
            json={"action": action, "version": data["version"]},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 409
    assert (
        owner.post(
            f"/api/applications/{data['id']}/actions",
            json={"action": "approve", "version": data["version"], "reason": "Bypass"},
            headers={"Idempotency-Key": str(uuid4())},
        ).status_code
        == 403
    )


def test_rate_limit(client, monkeypatch):
    monkeypatch.setenv("LOGIN_ACCOUNT_LIMIT", "3")
    email = f"missing-{uuid4()}@demo.local"
    codes = [client.post("/api/auth/login", json={"email": email, "password": "incorrect"}).status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]


def test_notification_ownership_and_bank_metrics(owner):
    data = act(owner, new_application(owner), "submit")
    note = next(n for n in owner.get("/api/notifications").json() if n["application_id"] == data["id"])
    assert owner.post(f"/api/notifications/{note['id']}/read").status_code == 200
    login(owner, "risk")
    assert owner.post(f"/api/notifications/{note['id']}/read").status_code == 404
    login(owner, "bank")
    summary = owner.get("/api/bank/summary").json()
    assert summary["total"] == len(owner.get("/api/applications").json())
    assert all(a["status"] in {"manual_review", "check_failed"} for a in owner.get("/api/applications?attention=true").json())
    assert all(a["product_id"] == "equipment" for a in owner.get("/api/applications?product_id=equipment").json())


def test_commit_failure_never_reports_success(owner, monkeypatch):
    from sqlalchemy.orm import Session

    with SessionLocal() as db:
        before = db.scalar(select(func.count()).select_from(Application))

    def fail_commit(self):
        raise RuntimeError("Synthetic commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", fail_commit)
        response = owner.post("/api/applications", json=PAYLOAD, headers={"Idempotency-Key": str(uuid4())})
        assert response.status_code == 500, response.text
        assert "id" not in response.json()
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Application)) == before


def test_chunked_body_is_bounded_before_parsing(owner):
    def chunks():
        for _ in range(8):
            yield b"x" * 32768

    response = owner.post("/api/applications", content=chunks(), headers={"Content-Type": "application/json"})
    assert response.status_code == 413


def test_transition_graph_blocks_internal_invalid_edge():
    from app.lending import transition

    draft = SimpleNamespace(status="draft", history=[{"status": "draft"}])
    with SessionLocal() as db, pytest.raises(HTTPException):
        transition(db, draft, "disbursed", "System", "Should fail")


def test_concurrent_disbursement_has_single_result(owner):
    data = act(owner, new_application(owner), "submit")
    data = act(owner, data, "accept")
    data = act(owner, data, "sign")

    def request(_):
        rival = TestClient(app)
        try:
            rival.cookies.update(owner.cookies)
            return rival.post(
                f"/api/applications/{data['id']}/actions",
                json={"action": "disburse", "version": data["version"]},
                headers={"X-CSRF-Token": owner.headers["X-CSRF-Token"], "Idempotency-Key": str(uuid4())},
            ).status_code
        finally:
            rival.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, range(2)))
    assert sorted(results) == [200, 409]
    saved = owner.get(f"/api/applications/{data['id']}").json()
    assert saved["status"] == "disbursed"
    assert sum(h["status"] == "disbursed" for h in saved["history"]) == 1


def test_concurrent_same_key_submit_replays_one_transaction(owner):
    data = new_application(owner)
    key = str(uuid4())

    def request(_):
        rival = TestClient(app)
        try:
            rival.cookies.update(owner.cookies)
            response = rival.post(
                f"/api/applications/{data['id']}/actions",
                json={"action": "submit", "version": data["version"]},
                headers={"X-CSRF-Token": owner.headers["X-CSRF-Token"], "Idempotency-Key": key},
            )
            assert response.status_code == 200, response.text
            return response.json()
        finally:
            rival.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, range(2)))
    assert results[0] == results[1]
    assert sum(h["status"] == "submitted" for h in results[0]["history"]) == 1
