"""Persistent credit-cycle API. All monetary values are integer kopecks.

A clock row serializes commands and worker claims. This deliberately conservative
lock order makes demo-clock/organization/account updates atomic on PostgreSQL;
SQLite mutations use the existing BEGIN IMMEDIATE dependency.
"""

import asyncio
import csv
import hashlib
import html
import io
import json
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import Response
from sqlalchemy import select
from pydantic import BaseModel, ConfigDict, Field
from .db import SessionLocal, SQLITE
from .models import Idempotency, now
from .security import get_db, get_user
from .v2_models import CreditRecord, CreditBlob
from .v2_seed import PROFILE_LABELS, profile_input, month_shift
from .credit_policy import PRODUCT_VERSIONS, POLICY_VERSION, evaluate_application, evaluate_cycle
from .credit_servicing import annuity_schedule, tranche_quote, repay_tranche

router = APIRouter(prefix="/api/v2", tags=["Кредитный цикл · DEMO_POLICY_1"])
TERMINAL = {"CONTRACT_ACTIVE", "NOT_ELIGIBLE", "DECLINED", "OFFER_REJECTED", "OFFER_EXPIRED", "WITHDRAWN"}
FORBIDDEN_INPUT = {
    "organization",
    "source_checks",
    "active_overdue_days",
    "closed_overdue_last12",
    "documents_complete",
    "documents_verified",
    "submitted_on",
}
DISCLAIMER = "Демонстрационные условия. Реальные операции не выполняются"


class CreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_id: str = "FIX-A"
    organization_id: str | None = None
    input: dict | None = None


class EditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    input: dict


class Command(BaseModel):
    model_config = ConfigDict(extra="allow")
    action: Literal[
        "consent",
        "revoke_consent",
        "submit",
        "respond",
        "save_reply",
        "claim",
        "release",
        "request_info",
        "accept_document",
        "decide",
        "retry",
        "reopen",
        "note",
        "accept",
        "decline",
        "sign",
        "withdraw",
        "refresh_checks",
        "draw_preview",
        "pay_preview",
        "repay_preview",
        "draw",
        "pay",
        "repay",
        "advance",
        "bind_scenario",
    ]
    expected_version: int = Field(ge=1)


def fail(code, message, status=422, **details):
    raise HTTPException(
        status,
        detail=message,
        headers={"X-Error-Code": code, **({"X-Current-Revision": str(details["revision"])} if "revision" in details else {})},
    )


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def load(db, key, kind=None):
    row = db.get(CreditRecord, key)
    if not row or (kind and row.kind != kind):
        fail("NOT_FOUND", "Объект не найден", 404)
    return row


def data(row):
    return {**deepcopy(row.data), "id": row.id, "revision": row.revision}


def save(row, value):
    row.revision += 1
    row.data = deepcopy(value)


def clock(db, lock=False):
    query = select(CreditRecord).where(CreditRecord.id == "demo-clock")
    if lock:
        query = query.with_for_update()
    return db.scalar(query)


def memberships(db, user):
    return [r for r in db.scalars(select(CreditRecord).where(CreditRecord.kind == "membership")).all() if r.data["user_id"] == user.id]


def access(db, user, oid, action="view"):
    if user.role == "bank":
        if action == "view":
            return {"roles": ["BANK_ANALYST"]}
        fail("ACCESS_DENIED", "Действие доступно уполномоченному представителю клиента", 403)
    if user.role == "demo_admin":
        fail("ACCESS_DENIED", "Демооператор не получает доступ к финансовым сведениям и решениям", 403)
    member = next((m for m in memberships(db, user) if m.organization_id == oid), None)
    if not member:
        fail("NOT_FOUND", "Объект не найден", 404)
    if action not in {"view", "prepare"}:
        today = clock(db).data["date"]
        if not member.data["valid_from"] <= today <= member.data["valid_to"] or action not in member.data["actions"]:
            fail("AUTHORITY_REQUIRED", "Нет действующего полномочия для этого действия. Обратитесь к подписанту организации", 403)
    return data(member)


def owned(db, user, key, kind="application", action="view"):
    row = load(db, key, kind)
    access(db, user, row.organization_id, action)
    return row


def bank(user):
    if user.role != "bank":
        fail("ACCESS_DENIED", "Доступно только аналитику банка", 403)


def assigned(user, app):
    bank(user)
    if app.get("assigned_analyst_id") != user.id:
        fail("ASSIGNMENT_REQUIRED", "Сначала возьмите заявку в работу. Решение доступно назначенному аналитику", 403)


def revision(row, expected, request=None):
    if request and request.headers.get("if-match"):
        if request.headers["if-match"].strip('"') != str(row.revision):
            fail("VERSION_CONFLICT", "Версия устарела. Обновите карточку; ваш ввод не перезаписан", 409, revision=row.revision)
    if row.revision != expected:
        fail("VERSION_CONFLICT", "Версия устарела. Обновите карточку; ваш ввод не перезаписан", 409, revision=row.revision)


def idempotent(db, user, request, scope, payload, run):
    # All writes take the clock lock before reading idempotency or aggregates.
    c = clock(db, True)
    key = request.headers.get("Idempotency-Key", "")
    if not 8 <= len(key) <= 128:
        fail("IDEMPOTENCY_REQUIRED", "Передайте Idempotency-Key длиной 8–128 символов", 422)
    hashed = digest(payload)
    previous = db.scalar(select(Idempotency).where(Idempotency.user_id == user.id, Idempotency.scope == scope, Idempotency.key == key))
    if previous:
        if previous.request_hash != hashed:
            fail("IDEMPOTENCY_CONFLICT", "Этот ключ уже использован для другого запроса", 409)
        return deepcopy(previous.response)
    result = run(c)
    db.add(Idempotency(user_id=user.id, scope=scope, key=key, request_hash=hashed, response=deepcopy(result)))
    db.flush()
    return result


def event(value, action, actor, c, **payload):
    value.setdefault("history", []).append(
        {
            "id": str(uuid4()),
            "action": action,
            "actor": actor,
            "occurred_at": c.data["now"],
            "recorded_at": now().isoformat(),
            "payload": deepcopy(payload),
        }
    )
    value["updated_at"] = c.data["now"]


def job(db, kind, target, oid, **details):
    key = str(uuid4())
    row = CreditRecord(
        id=key,
        kind="job",
        organization_id=oid,
        revision=1,
        data={
            "kind": kind,
            "target_id": target,
            "status": "PENDING",
            "attempt": 0,
            "available_at": now().isoformat(),
            "history": [],
            **details,
        },
    )
    db.add(row)
    return key


def safe_input(value):
    # Keep absence distinct from zero; reject all binary float money, bool-as-int,
    # negative values, large arrays and unbounded nested objects at the boundary.
    if len(json.dumps(value, ensure_ascii=False)) > 110000:
        fail("INPUT_TOO_LARGE", "Слишком много финансовых периодов или событий")

    def visit(item, key="", depth=0):
        if depth > 10:
            fail("INVALID_INPUT", "Слишком большая вложенность данных")
        if key.endswith("_kopecks") and item is not None:
            if type(item) is not int or not 0 <= item <= 10000000000000:
                fail("INVALID_MONEY", f"{key}: нужна неотрицательная сумма целыми копейками")
        if isinstance(item, dict):
            for k, v in item.items():
                visit(v, k, depth + 1)
        elif isinstance(item, list):
            if len(item) > 500:
                fail("INVALID_INPUT", "Слишком много строк плана")
            for v in item:
                visit(v, key, depth + 1)
        elif isinstance(item, float):
            fail("INVALID_INPUT", "Передавайте суммы целыми копейками, доли — базисными пунктами")

    visit(value)
    if value.get("product_code") not in {"DEMO_TERM_LOAN_V1", "DEMO_REVOLVING_V1"}:
        fail("PRODUCT_REQUIRED", "Выберите один из двух демонстрационных продуктов")
    if type(value.get("amount_kopecks")) is not int or type(value.get("term_months")) is not int:
        fail("INVALID_INPUT", "Укажите сумму в копейках и срок в месяцах")
    if not 1 <= value["term_months"] <= 24:
        fail("INVALID_INPUT", "Недопустимый срок")
    if value.get("concentration_bps") is not None and (
        type(value["concentration_bps"]) is not int or not 0 <= value["concentration_bps"] <= 10000
    ):
        fail("INVALID_INPUT", "Доля крупнейшего покупателя должна быть от 0 до 100%")
    for periods in (value.get("history", []), value.get("forecast", [])):
        if not isinstance(periods, list) or len(periods) > 24:
            fail("INVALID_INPUT", "Нужно не более 24 финансовых периодов")
        for p in periods:
            try:
                date.fromisoformat(str(p.get("period", "")) + "-01")
            except (ValueError, TypeError, AttributeError):
                fail("INVALID_PERIOD", "Укажите период в формате ГГГГ-ММ")
    return deepcopy(value)


MONTHLY_CASH_FIELDS = (
    ("CUSTOMER_RECEIPT", "customer_receipts_kopecks"),
    ("OPERATING_PAYMENT", "operating_payments_kopecks"),
    ("TAX_PAYMENT", "taxes_kopecks"),
    ("MAINTENANCE_CAPEX", "maintenance_capex_kopecks"),
    ("MANDATORY_WITHDRAWAL", "mandatory_withdrawals_kopecks"),
    ("OLD_DEBT_PAYMENT", "old_debt_service_kopecks"),
)


def organization_contracts(db, oid):
    return db.scalars(select(CreditRecord).where(CreditRecord.kind == "contract", CreditRecord.organization_id == oid)).all()


def cash_source(event, contract):
    """Business source identity survives contract IDs and payment-row dates."""
    if event.get("cash_source"):
        return event["cash_source"]
    if event.get("source_event_id"):
        return source_identity(event["source_event_id"])
    if contract.get("scheduled"):
        return "monthly:" + event.get("period", event["date"][:7]) + ":" + event["kind"]
    return source_identity(event["id"])


def source_identity(identifier):
    if not isinstance(identifier, str) or not identifier:
        fail("INVALID_CYCLE_EVENT", "Денежному событию нужен непустой идентификатор")
    return identifier if identifier.startswith("monthly:") else "source:" + identifier


def organization_cash_sources(db, oid):
    sources = {}
    for row in organization_contracts(db, oid):
        events = list(row.data.get("cash_events", []))
        for tranche in row.data.get("tranches", []):
            if tranche["status"] in {"RESERVED", "DISBURSING"}:
                events += [e for e in tranche["cycle_plan"]["events"] if e["kind"] != "TARGET_PAYMENT"]
        for event in events:
            identity = cash_source(event, row.data)
            signature = tuple(event[k] for k in ("date", "kind", "amount_kopecks"))
            previous = sources.get(identity)
            if previous and previous["signature"] != signature:
                fail("CASHFLOW_CONFLICT", "Сохраненные договоры содержат противоречивые общие потоки; требуется уточнение", 409)
            sources[identity] = {"signature": signature, "event": deepcopy(event), "executed": bool(event.get("executed")) or bool(previous and previous["executed"])}
    return sources


def shared_cycle_plan(db, oid, plan, c, horizon_days=90):
    """One organization's dated cash and debt cannot be allocated per facility."""
    plan = deepcopy(plan)
    sources = organization_cash_sources(db, oid)
    horizon = (date.fromisoformat(c.data["date"]) + timedelta(days=horizon_days)).isoformat()
    incoming = {source_identity(e["id"]): e for e in plan.get("events", []) if e.get("kind") != "TARGET_PAYMENT"}
    for identity, prior in sources.items():
        event = prior["event"]
        candidate = incoming.get(identity)
        if candidate and tuple(candidate[k] for k in ("date", "kind", "amount_kopecks")) != prior["signature"]:
            fail("CASHFLOW_CONFLICT", "Общее поступление или расход организации уже подтверждены с другой суммой/датой", 409)
        if candidate and prior["executed"]:
            fail("RECEIPT_ALREADY_USED" if candidate["kind"] == "CUSTOMER_RECEIPT" else "CASHFLOW_ALREADY_USED", "Общее событие уже проведено и учтено в остатке счета", 409)
        if not candidate and not prior["executed"] and c.data["date"] <= event["date"] <= horizon:
            plan.setdefault("events", []).append({"id": identity.removeprefix("source:"), **{k: event[k] for k in ("date", "kind", "amount_kopecks")}})
    commitments = []
    for row in organization_contracts(db, oid):
        cd = row.data
        for payment in (cd.get("scheduled") or {}).get("rows", []):
            if payment["status"] != "PAID":
                commitments.append({"id": "debt:" + row.id + ":" + str(payment["number"]), "date": max(c.data["date"], payment["due_on"]), "total_kopecks": payment["payment_kopecks"]})
        for tranche in cd.get("tranches", []):
            if tranche["status"] in {"ACTIVE", "OVERDUE", "RESERVED", "DISBURSING"}:
                due_on = max(c.data["date"], tranche["matures_on"])
                debt = tranche_quote({**tranche, "principal_kopecks": tranche["amount_kopecks"]} if tranche["status"] in {"RESERVED", "DISBURSING"} else tranche, due_on)
                commitments.append({"id": "debt:" + tranche["id"], "date": due_on, "total_kopecks": debt["total_kopecks"]})
    plan["existing_repayments"] = commitments
    return plan


def monthly_cash_events(forecast, issued_on, org):
    for index, period in enumerate(forecast, 1):
        due_on = month_shift(issued_on, index)
        for kind, field in MONTHLY_CASH_FIELDS:
            amount = period.get(field, 0)
            if kind == "OLD_DEBT_PAYMENT":
                amount = org["baseline_old_debt_kopecks"] if due_on <= org["baseline_old_debt_end_on"] else 0
            yield {"cash_source": "monthly:" + period["period"] + ":" + kind,
                   "period": period["period"], "date": due_on, "kind": kind,
                   "amount_kopecks": amount, "executed": False}


def evaluation_cycle_plan(plan):
    plan = deepcopy(plan)
    for event in plan.get("events", []):
        if event.get("kind") in {"MAINTENANCE_CAPEX", "MANDATORY_WITHDRAWAL"}:
            event["kind"] = "OTHER_PAYMENT"
    return plan


def evaluate_normalized(value):
    payload = deepcopy(value)
    if payload.get("product_code") == "DEMO_REVOLVING_V1":
        payload["cycle_plan"] = evaluation_cycle_plan(payload["cycle_plan"])
    assessment = evaluate_application(payload)
    if value.get("cashflow_conflicts"):
        assessment.update(status="NEEDS_INFO", decision=None, offer=None, manual_approval_allowed=False,
                          reasons=[{"code": "CASHFLOW_CONFLICT", "message": "Общий прогноз организации уже подтвержден с другими датами или суммами; уточните источники, не создавая повторные поступления", "field": "forecast", "sources": value["cashflow_conflicts"]}])
    return assessment


def normalized(db, app, c):
    value = deepcopy(app["input"])
    org = load(db, app["organization_id"], "organization").data
    value["organization"] = {k: org[k] for k in ("legal_form", "registration_date", "status")}
    value["submitted_on"] = c.data["date"]
    value["active_overdue_days"] = org.get("active_overdue_days", 0)
    value["closed_overdue_last12"] = org.get("closed_overdue_last12", False)
    value["documents_complete"] = all(
        any(d["kind"] == kind and d["status"] != "SUPERSEDED" for d in app["documents"]) for kind in required_docs(org)
    )
    value["documents_verified"] = all(
        d["status"] in {"VALIDATED", "ACCEPTED_BY_ANALYST", "SUPERSEDED", "NOT_APPLICABLE"} for d in app["documents"]
    )
    current_facts = [
        {k: p.get(k) for k in ("period", "customer_receipts_kopecks", "operating_payments_kopecks")} for p in value.get("history", [])
    ]
    for document in app["documents"]:
        if (
            document["kind"] in {"statement", "financial_plan"}
            and document["status"] == "VALIDATED"
            and document.get("structured_history") != current_facts
        ):
            if not (app.get("scenario_id") == "FIX-H" and app.get("reconciliation_confirmed") and document["kind"] == "statement"):
                value["documents_verified"] = False
    latest = {}
    for check in app["checks"]:
        latest[check["source"]] = check
    value["source_checks"] = {
        name: (
            latest[name]["result"]
            if name in latest and latest[name]["status"] == "SUCCEEDED" and latest[name]["valid_until"] > c.data["now"]
            else "UNAVAILABLE"
        )
        for name in ("registry", "credit_history", "control")
    }
    if app.get("scenario_id") == "FIX-E" and not app.get("statement_completed"):
        value["documents_complete"] = False
    if app.get("scenario_id") == "FIX-H" and not app.get("reconciliation_confirmed"):
        value["documents_complete"] = False
    # Canonical external obligation + all internal active contracts. Client edits
    # never erase known debt; a corresponding BKI ID is counted exactly once.
    contracts = organization_contracts(db, app["organization_id"])
    known_sources = organization_cash_sources(db, app["organization_id"])
    conflicts = []
    if value["product_code"] == "DEMO_TERM_LOAN_V1":
        for candidate in monthly_cash_events(value.get("forecast", []), c.data["date"], org):
            prior = known_sources.get(candidate["cash_source"])
            if prior and prior["signature"] != tuple(candidate[k] for k in ("date", "kind", "amount_kopecks")):
                conflicts.append(candidate["cash_source"])
    value["cashflow_conflicts"] = conflicts
    value["available_own_funds_kopecks"] = load(db, "account:" + app["organization_id"]).data["balance_kopecks"]
    value["already_committed_own_funds_kopecks"] = 0
    if app.get("statement_classification"):
        value["statement_classification"] = deepcopy(app["statement_classification"])
    value["obligations"] = []
    for index, p in enumerate(value.get("forecast", []), 1):
        from_on = month_shift(c.data["date"], index - 1)
        to_on = month_shift(c.data["date"], index)
        baseline = org["baseline_old_debt_kopecks"] if to_on <= org["baseline_old_debt_end_on"] else 0
        internal = 0
        for contract in contracts:
            cd = contract.data
            if cd.get("status") == "CLOSED":
                continue
            for row in (cd.get("scheduled") or {}).get("rows", []):
                if row["status"] != "PAID" and from_on < row["due_on"] <= to_on:
                    internal += row["payment_kopecks"]
            for tr in cd.get("tranches", []):
                if tr["status"] in {"ACTIVE", "OVERDUE"} and from_on < tr["matures_on"] <= to_on:
                    quote = tranche_quote(tr, tr["matures_on"])
                    internal += quote["total_kopecks"]
        p["old_debt_service_kopecks"] = max(p.get("old_debt_service_kopecks") or 0, baseline) + internal
    value["obligations"] = [
        {
            "canonical_id": "external-seed:" + app["organization_id"],
            "source": "CreditHistoryMock",
            "monthly_kopecks": org["baseline_old_debt_kopecks"],
            "ends_on": org["baseline_old_debt_end_on"],
        }
    ] + [{"canonical_id": r.id, "source": "InternalContract"} for r in contracts if r.data["status"] != "CLOSED"]
    if value["product_code"] == "DEMO_REVOLVING_V1":
        plan = value["cycle_plan"]
        commitments = []
        end_on = (date.fromisoformat(c.data["date"]) + timedelta(days=90)).isoformat()
        for contract in contracts:
            cd = contract.data
            for payment in (cd.get("scheduled") or {}).get("rows", []):
                if payment["status"] != "PAID" and payment["due_on"] <= end_on:
                    commitments.append(
                        {
                            "id": contract.id + ":" + str(payment["number"]),
                            "date": max(c.data["date"], payment["due_on"]),
                            "total_kopecks": payment["payment_kopecks"],
                        }
                    )
            for tranche in cd.get("tranches", []):
                if tranche["status"] in {"ACTIVE", "OVERDUE"} and tranche["matures_on"] <= end_on:
                    due_on = max(c.data["date"], tranche["matures_on"])
                    commitments.append(
                        {"id": tranche["id"], "date": due_on, "total_kopecks": tranche_quote(tranche, due_on)["total_kopecks"]}
                    )
        plan["existing_repayments"] = commitments
        try:
            plan = shared_cycle_plan(db, app["organization_id"], plan, c)
        except HTTPException as exc:
            value["cashflow_conflicts"].append(str(exc.detail))
        value["cycle_plan"] = plan
        known_ids = {e["id"] for e in plan.get("events", [])}
        for index in range(1, 4):
            due_on = month_shift(c.data["date"], index)
            canonical = "external-seed:" + app["organization_id"] + ":" + due_on
            if due_on <= min(end_on, org["baseline_old_debt_end_on"]) and canonical not in known_ids and org["baseline_old_debt_kopecks"]:
                plan["events"].append(
                    {"id": canonical, "date": due_on, "kind": "OLD_DEBT_PAYMENT", "amount_kopecks": org["baseline_old_debt_kopecks"]}
                )
    return value


def required_docs(org):
    return ["statement", "financial_plan", "purpose", "authority"] + (["annual_report"] if org["legal_form"] == "OOO" else [])


def check_fresh(app, c):
    latest = {r["source"]: r for r in app["checks"]}
    return all(
        name in latest
        and latest[name]["status"] == "SUCCEEDED"
        and latest[name]["valid_until"] > c.data["now"]
        and latest[name]["result"] == "CLEAR"
        for name in ("registry", "credit_history", "control")
    )


def public_app(row, user):
    a = data(row)
    if user.role != "bank":
        def clean_private(value):
            if isinstance(value, dict):
                for key in ("internal_notes", "internal_reason"):
                    value.pop(key, None)
                if "outcome" in value and "public_explanation" in value:
                    value.pop("reason", None)
                if "revoked_reason" in value:
                    value["revoked_reason"] = "Условия отозваны для повторной проверки; требуется актуальная версия"
                if value.get("action") == "OfferRevoked" and isinstance(value.get("payload"), dict):
                    value["payload"]["reason"] = "Условия отозваны для повторной проверки"
                for nested in value.values():
                    clean_private(nested)
            elif isinstance(value, list):
                for nested in value:
                    clean_private(nested)

        clean_private(a)
        # Closed control findings stay private; the customer receives an allowed
        # process explanation instead of the fixture/heuristic.
        for check in a.get("checks", []):
            if check["source"] == "control":
                check["result"] = "CLEAR" if check.get("result") == "CLEAR" else "Дополнительная проверка"
        a.pop("scenario_id", None)
    return a


def manifest_documents(db, a, c):
    org = load(db, a["organization_id"]).data
    docs = []
    for kind in required_docs(org):
        key = str(uuid4())
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["synthetic_id", "document_kind", "period", "customer_receipts_kopecks", "operating_payments_kopecks"])
        history = a["input"]["history"][:-1] if a["scenario_id"] == "FIX-E" and kind == "statement" else a["input"]["history"]
        for p in history:
            writer.writerow(
                [
                    org["synthetic_id"],
                    kind,
                    p["period"],
                    500000000 if a.get("scenario_id") == "FIX-H" and kind == "statement" else p["customer_receipts_kopecks"],
                    p["operating_payments_kopecks"],
                ]
            )
        content = output.getvalue().encode("utf-8")
        docs.append(
            {
                "id": key,
                "kind": kind,
                "name": f"{org['synthetic_id']}-{kind}.csv",
                "media_type": "text/csv",
                "size": len(content),
                "hash": hashlib.sha256(content).hexdigest(),
                "status": "VALIDATED",
                "version": 1,
                "source": "PublishedSyntheticManifest",
                "periods": [p["period"] for p in history],
                "structured_history": [
                    {k: p.get(k) for k in ("period", "customer_receipts_kopecks", "operating_payments_kopecks")} for p in history
                ],
                "created_at": c.data["now"],
                "synthetic": True,
            }
        )
        if a["scenario_id"] == "FIX-H" and kind == "statement":
            for fact in docs[-1]["structured_history"]:
                fact["customer_receipts_kopecks"] = 500000000
        db.add(CreditBlob(id=key, application_id=a["id"], content=content))
    return docs


@router.get("/workspace")
def workspace(organization_id: str | None = None, user=Depends(get_user), db=Depends(get_db, scope="function")):
    c = clock(db)
    if user.role == "demo_admin":
        return {
            "clock": data(c),
            "roles": ["DEMO_ADMIN"],
            "organizations": [],
            "organization": None,
            "products": [],
            "applications": [
                {"id": r.id, "number": r.data["number"], "status": r.data["status"], "revision": r.revision}
                for r in db.scalars(select(CreditRecord).where(CreditRecord.kind == "application")).all()
            ],
            "contracts": [],
            "account": None,
            "templates": [],
            "scenarios": [{"id": "FIX-" + k, "label": v} for k, v in PROFILE_LABELS.items()]
            + [
                {"id": "SOURCE_ALWAYS_TIMEOUT", "label": "Четыре недоступных ответа БКИ"},
                {"id": "EXECUTION_FAIL_ONCE", "label": "Один технический сбой исполнения"},
                {"id": "TRANCHE_FAIL_FINAL", "label": "Неуспешная выдача транша"},
            ],
            "jobs": [data(r) for r in db.scalars(select(CreditRecord).where(CreditRecord.kind == "job")).all()],
        }
    members = memberships(db, user)
    oids = [m.organization_id for m in members]
    if user.role == "bank":
        organizations = [data(r) for r in db.scalars(select(CreditRecord).where(CreditRecord.kind == "organization")).all()]
        selected = None
        roles = ["BANK_ANALYST"]
    else:
        if organization_id and organization_id not in oids:
            fail("NOT_FOUND", "Организация не найдена", 404)
        selected = organization_id or (oids[0] if oids else None)
        organizations = [data(load(db, oid)) for oid in oids]
        roles = next((m.data["roles"] for m in members if m.organization_id == selected), [])
    org = next((o for o in organizations if o["id"] == selected), None)
    query = select(CreditRecord).where(CreditRecord.kind.in_(["application", "contract"]))
    if selected:
        query = query.where(CreditRecord.organization_id == selected)
    elif user.role != "bank":
        return {
            "clock": data(c),
            "roles": roles,
            "organization": None,
            "organizations": [],
            "applications": [],
            "contracts": [],
            "products": list(PRODUCT_VERSIONS.values()),
            "templates": [],
            "account": None,
        }
    rows = db.scalars(query.order_by(CreditRecord.created_at.desc(), CreditRecord.id)).all()
    templates = []
    if org:
        letter = org["synthetic_id"][-1]
        template = profile_input(letter, c.data["date"])
        template["organization"] = {k: org[k] for k in ("legal_form", "registration_date", "status")}
        templates = [
            {
                "id": org["synthetic_id"],
                "label": PROFILE_LABELS[letter],
                "description": PROFILE_LABELS[letter],
                "organization_id": org["id"],
                "input": template,
            }
        ]
    return {
        "clock": data(c),
        "roles": roles,
        "organizations": organizations,
        "organization": org,
        "products": list(PRODUCT_VERSIONS.values()),
        "applications": [public_app(r, user) for r in rows if r.kind == "application"],
        "contracts": [contract_view(db, r, c) for r in rows if r.kind == "contract"],
        "templates": templates,
        "account": data(load(db, "account:" + selected)) if selected else None,
        "scenarios": [],
        "disclaimer": DISCLAIMER,
    }


@router.get("/products")
def products(user=Depends(get_user)):
    return {"products": list(PRODUCT_VERSIONS.values()), "policy_version": POLICY_VERSION, "disclaimer": DISCLAIMER}


@router.get("/applications")
def applications(
    user=Depends(get_user),
    db=Depends(get_db, scope="function"),
    organization_id: str | None = None,
    page: int = 1,
    page_size: int = 25,
    status: str | None = None,
):
    result = workspace(organization_id, user, db)["applications"]
    if status:
        result = [a for a in result if a["status"] == status]
    page_size = max(1, min(page_size, 50))
    page = max(1, page)
    return {"items": result[(page - 1) * page_size : page * page_size], "total": len(result), "page": page, "page_size": page_size}


@router.post("/applications", status_code=201)
def create(body: CreateRequest, request: Request, user=Depends(get_user), db=Depends(get_db, scope="function")):
    def run(c):
        members = memberships(db, user)
        oid = body.organization_id or (members[0].organization_id if members else None)
        access(db, user, oid, "prepare")
        org = load(db, oid, "organization").data
        if body.template_id != org["synthetic_id"]:
            fail("TEMPLATE_MISMATCH", "Шаблон относится к другой организации")
        value = profile_input(org["synthetic_id"][-1], c.data["date"])
        value["organization"] = {k: org[k] for k in ("legal_form", "registration_date", "status")}
        if body.input is not None:
            incoming = safe_input(body.input)
            for k, v in incoming.items():
                if k not in FORBIDDEN_INPUT:
                    value[k] = v
        aid = str(uuid4())
        a = {
            "id": aid,
            "number": "КЦ-" + aid[:8].upper(),
            "organization_id": oid,
            "organization_name": org["name"],
            "status": "DRAFT",
            "phase": None,
            "input": value,
            "scenario_id": org["scenario_id"],
            "checks": [],
            "assessment": None,
            "offers": [],
            "offer": None,
            "clarifications": [],
            "documents": [],
            "history": [],
            "versions": [],
            "decisions": [],
            "consents": [],
            "assigned_analyst_id": None,
            "contract_id": None,
            "bundle": None,
            "acceptance": None,
            "signing": None,
            "internal_notes": [],
            "created_at": c.data["now"],
        }
        row = CreditRecord(id=aid, kind="application", organization_id=oid, revision=1, data=deepcopy(a))
        db.add(row)
        db.flush()
        a["documents"] = manifest_documents(db, a, c)
        event(a, "ApplicationDraftCreated", user.name, c)
        row.data = deepcopy(a)
        return public_app(row, user)

    return idempotent(db, user, request, "v2:create", body.model_dump(), run)


@router.get("/applications/{application_id}")
def get_application(application_id: str, user=Depends(get_user), db=Depends(get_db, scope="function")):
    return public_app(owned(db, user, application_id), user)


@router.put("/applications/{application_id}")
@router.patch("/applications/{application_id}/draft")
def edit(application_id: str, body: EditRequest, request: Request, user=Depends(get_user), db=Depends(get_db, scope="function")):
    def run(c):
        row = owned(db, user, application_id, action="prepare")
        revision(row, body.expected_version, request)
        a = deepcopy(row.data)
        if a["status"] != "DRAFT":
            fail("INVALID_STATE", "Изменять анкету можно в черновике или через ответ на уточнение", 409)
        incoming = safe_input(body.input)
        a["input"] = {**a["input"], **{k: v for k, v in incoming.items() if k not in FORBIDDEN_INPUT}}
        event(a, "DraftSaved", user.name, c)
        save(row, a)
        return public_app(row, user)

    return idempotent(db, user, request, "v2:edit:" + application_id, body.model_dump(), run)


def clarification(a, c, question, fields, actor="SYSTEM", code="DATA_MISSING"):
    opened = next((x for x in a["clarifications"] if x["status"] in {"OPEN", "DRAFT_REPLY"} and x["code"] == code), None)
    if opened:
        return
    a["clarifications"].append(
        {
            "id": str(uuid4()),
            "question": question,
            "fields": fields,
            "code": code,
            "status": "OPEN",
            "author": actor,
            "created_at": c.data["now"],
            "replies": [],
        }
    )
    event(a, "ClarificationRequested", actor, c, question=question, fields=fields, code=code)


def snapshot_application(a, c, user):
    a["versions"].append(
        {
            "id": str(uuid4()),
            "version": len(a["versions"]) + 1,
            "input": deepcopy(a["input"]),
            "document_ids": [d["id"] for d in a["documents"] if d["status"] != "SUPERSEDED"],
            "consent_ids": [r["id"] for r in a["consents"] if r["status"] == "ACTIVE"],
            "hash": digest(a["input"]),
            "submitted_by": user.id,
            "submitted_at": c.data["now"],
            "policy_version": POLICY_VERSION,
            "product_version": a["input"]["product_code"],
        }
    )


def invalidate_offer(a, c, reason):
    if a.get("offer") and a["offer"]["status"] in {"ACTIVE", "ACCEPTED"}:
        a["offer"]["status"] = "REVOKED"
        a["offer"]["revoked_reason"] = reason
        for offer in a["offers"]:
            if offer["id"] == a["offer"]["id"]:
                offer["status"] = "REVOKED"
        event(a, "OfferRevoked", "SYSTEM", c, reason=reason)
    if a.get("acceptance"):
        a["acceptance"] = {**a["acceptance"], "status": "INVALIDATED", "reason": reason}
    a["bundle"] = None


def publish_offer(db, a, c, assessment, actor="SYSTEM", explanation=None):
    candidate = deepcopy(assessment["offer"])
    decision = {
        "id": str(uuid4()),
        "outcome": assessment.get("decision") or "APPROVE",
        "actor": actor,
        "internal_reason": explanation or "Все обязательные правила выполнены",
        "public_explanation": explanation
        or (
            "Подтвержденный поток покрывает все платежи"
            if assessment.get("decision") == "APPROVE"
            else "Более долгий срок снижает платеж и увеличивает общие проценты. Сравните условия перед принятием"
        ),
        "application_version": len(a["versions"]),
        "policy_version": POLICY_VERSION,
        "product_version": a["input"]["product_code"],
        "assessment_id": assessment["id"],
        "input_hash": assessment["input_hash"],
        "created_at": c.data["now"],
    }
    decision["hash"] = digest(decision)
    a["decisions"].append(decision)
    org = load(db, a["organization_id"]).data
    candidate.update(
        id=str(uuid4()),
        version=len(a["offers"]) + 1,
        status="ACTIVE",
        created_at=c.data["now"],
        expires_at=(datetime.fromisoformat(c.data["now"]) + timedelta(hours=168)).isoformat(),
        planned_on=c.data["date"],
        organization_id=a["organization_id"],
        decision_id=decision["id"],
        policy_version=POLICY_VERSION,
        obligations_revision=org["obligations_revision"],
        explanation=decision["public_explanation"],
        disclaimer=DISCLAIMER,
    )
    candidate["hash"] = digest(candidate)
    a["offer"] = deepcopy(candidate)
    a["offers"].append(candidate)
    a["status"] = "OFFER_READY"
    a["phase"] = None
    event(a, "DecisionRecorded", actor, c, outcome=decision["outcome"], decision_id=decision["id"])
    event(a, "OfferPublished", actor, c, offer_id=candidate["id"], offer_version=candidate["version"])


def pipeline(db, row, c):
    a = deepcopy(row.data)
    if a["status"] not in {"SUBMITTED", "PROCESSING", "WAITING_SOURCE", "MANUAL_REVIEW"}:
        return True
    if not any(r["purpose"] == "CREDIT_HISTORY" and r["status"] == "ACTIVE" and r["valid_until"] >= c.data["date"] for r in a["consents"]):
        a["status"] = "NEEDS_INFO"
        clarification(
            a,
            c,
            "Нужно обновить отдельное согласие на демопроверку кредитной истории",
            ["consents.CREDIT_HISTORY"],
            code="CONSENT_REQUIRED",
        )
        save(row, a)
        return True
    a["status"] = "PROCESSING"
    preflight_input = normalized(db, a, c)
    preflight = evaluate_normalized(preflight_input)
    if preflight["status"] in {"NOT_ELIGIBLE", "NEEDS_INFO"}:
        return complete_assessment(db, row, a, c, preflight_input, preflight)
    a["phase"] = "SOURCES"
    attempt = a.get("source_attempt", 0) + 1
    a["source_attempt"] = attempt
    scenario = a["scenario_id"]
    unavailable = (scenario == "FIX-I" and attempt <= 2) or scenario == "SOURCE_ALWAYS_TIMEOUT"
    for source in ("registry", "credit_history", "control"):
        successful = not (source == "credit_history" and unavailable)
        # Reuse a source only for the same source-specific input and fresh result.
        source_input = {"organization_id": a["organization_id"], "source": source}
        if source == "credit_history":
            source_input["consent_ids"] = [r["id"] for r in a["consents"] if r["purpose"] == "CREDIT_HISTORY" and r["status"] == "ACTIVE"]
        input_hash = digest(source_input)
        previous = next((r for r in reversed(a["checks"]) if r["source"] == source), None)
        if (
            previous
            and previous["input_hash"] == input_hash
            and previous["status"] == "SUCCEEDED"
            and previous["valid_until"] > c.data["now"]
        ):
            continue
        check = {
            "id": str(uuid4()),
            "source": source,
            "attempt": attempt,
            "application_version": len(a["versions"]),
            "input_hash": input_hash,
            "fetched_at": c.data["now"],
            "valid_until": (datetime.fromisoformat(c.data["now"]) + timedelta(days=7)).isoformat(),
            "status": "SUCCEEDED" if successful else "FAILED_RETRYABLE" if attempt < 4 else "FAILED_FINAL",
            "result": "CLEAR" if successful else None,
            "technical_code": None if successful else "TIMEOUT",
            "synthetic": True,
        }
        a["checks"].append(check)
        event(a, "CheckCompleted" if successful else "CheckFailed", "SYSTEM", c, source=source, attempt=attempt, status=check["status"])
    if unavailable:
        a["status"] = "WAITING_SOURCE" if attempt < 4 else "MANUAL_REVIEW"
        a["phase"] = "SOURCES"
        if attempt >= 4:
            clarification(
                a,
                c,
                "БКИ не ответило после четырёх попыток. Аналитику требуется повторить техническую проверку; экономическое решение пока недоступно",
                ["source:credit_history"],
                code="SOURCE_UNAVAILABLE",
            )
        save(row, a)
        return attempt >= 4
    value = normalized(db, a, c)
    return complete_assessment(db, row, a, c, value, evaluate_normalized(value))


def complete_assessment(db, row, a, c, value, assessment):
    scenario = a["scenario_id"]
    if scenario == "FIX-H" and not a.get("reconciliation_confirmed") and assessment["status"] == "NEEDS_INFO":
        assessment["reasons"] = [
            {
                "code": "RECONCILIATION_REQUIRED",
                "message": "Покупательские поступления требуют классификации: исходная выписка 30 млн, заявлено 24 млн",
                "field": "statement_classification",
            }
        ]
    assessment.update(
        id=str(uuid4()),
        created_at=c.data["now"],
        input_hash=digest(value),
        input_snapshot=value,
        application_version=len(a["versions"]),
        check_ids=[r["id"] for r in a["checks"] if r["status"] == "SUCCEEDED"],
    )
    a.setdefault("assessments", []).append(deepcopy(assessment))
    a["assessment"] = assessment
    a["status"] = assessment["status"]
    a["phase"] = None
    event(
        a,
        "AssessmentCalculated",
        "SYSTEM",
        c,
        assessment_id=assessment["id"],
        result=assessment["status"],
        risk=assessment["risk"]["category"],
    )
    if a["status"] == "NEEDS_INFO":
        if scenario == "FIX-E" and not a.get("statement_completed"):
            question = "В синтетической выписке нет последнего завершенного месяца. Добавьте этот период и отправьте подтвержденный ответ"
            fields = ["documents.statement.last_period"]
        elif scenario == "FIX-H" and not a.get("reconciliation_confirmed"):
            question = "В исходной выписке 30 млн ₽: подтвердите классификацию 3 млн собственных переводов, 2 млн кредита и 1 млн возврата депозита. Покупательские поступления составят 24 млн ₽"
            fields = ["statement_classification"]
        else:
            question = "; ".join(r["message"] for r in assessment["reasons"][:4])
            fields = [r.get("field", r["code"]) for r in assessment["reasons"]]
        clarification(a, c, question, fields)
    elif a["status"] == "OFFER_READY":
        publish_offer(db, a, c, assessment)
    elif a["status"] == "MANUAL_REVIEW":
        event(a, "ManualReviewRequested", "SYSTEM", c, reasons=[r["code"] for r in assessment["reasons"]])
    save(row, a)
    return True


def bundle_for(a, c):
    offer = a["offer"]
    terms = {
        "product": offer["product_code"],
        "amount_kopecks": offer["amount_kopecks"],
        "annual_rate_bps": offer["annual_rate_bps"],
        "term_months": offer["term_months"],
        "planned_on": offer["planned_on"],
        "fees_kopecks": 0,
        "offer_hash": offer["hash"],
    }
    docs = []
    for title, content in [
        ("Демонстрационный договор", json.dumps(terms, ensure_ascii=False, indent=2)),
        (
            "График платежей" if offer["product_code"] == "DEMO_TERM_LOAN_V1" else "Лимит и правила траншей",
            json.dumps(offer["calculation"], ensure_ascii=False, indent=2),
        ),
        ("Сводка согласий", json.dumps(a["consents"], ensure_ascii=False, indent=2)),
        (
            "Объяснение симуляции",
            "Это демонстрация, юридически значимая электронная подпись не создается. Денежные переводы не выполняются. " + DISCLAIMER,
        ),
    ]:
        markup = "<article><h2>" + html.escape(title) + "</h2><p>" + DISCLAIMER + "</p><pre>" + html.escape(content) + "</pre></article>"
        docs.append({"title": title, "content": content, "html": markup, "hash": hashlib.sha256(markup.encode()).hexdigest()})
    result = {
        "id": str(uuid4()),
        "offer_id": offer["id"],
        "offer_hash": offer["hash"],
        "acceptance_id": a["acceptance"]["id"],
        "planned_on": offer["planned_on"],
        "documents": docs,
        "html": "".join(r["html"] for r in docs),
        "created_at": c.data["now"],
    }
    result["hash"] = digest(result)
    return result


def confirm_offer(db, a, c, body):
    offer = a.get("offer")
    if (
        not offer
        or offer["status"] not in {"ACTIVE", "ACCEPTED"}
        or body.get("offer_id") != offer["id"]
        or body.get("offer_hash") != offer["hash"]
    ):
        fail("OFFER_STALE", "Открыта устаревшая версия условий. Обновите предложение", 409)
    if body.get("confirmed") is not True:
        fail("CONFIRMATION_REQUIRED", "Подтвердите условия отдельным действием")
    if a["status"] == "OFFER_READY" and offer["expires_at"] <= c.data["now"]:
        fail("OFFER_EXPIRED", "Срок предложения истек. Создайте новую заявку", 409)
    org = load(db, a["organization_id"]).data
    changed_date = offer["planned_on"] != c.data["date"]
    stale = not check_fresh(a, c) or offer["obligations_revision"] != org["obligations_revision"]
    expired_acceptance = a.get("acceptance") and a["acceptance"]["sign_by"] <= c.data["now"]
    if changed_date or stale or expired_acceptance:
        invalidate_offer(a, c, "Изменились календарь, обязательства или свежесть оснований; требуется новое принятие")
        a["status"] = "PROCESSING"
        a["source_attempt"] = 0
        return False
    return True


@router.post("/applications/{application_id}/commands")
def commands(application_id: str, body: Command, request: Request, user=Depends(get_user), db=Depends(get_db, scope="function")):
    b = body.model_dump()
    action = b["action"]

    def run(c):
        row = owned(db, user, application_id)
        revision(row, body.expected_version, request)
        a = deepcopy(row.data)
        oid = row.organization_id
        if action in {"claim", "release", "request_info", "decide", "retry", "reopen", "note", "accept_document"}:
            bank(user)
        else:
            scope = {
                "consent": "consent",
                "submit": "submit",
                "respond": "submit",
                "save_reply": "prepare",
                "accept": "accept",
                "decline": "decline",
                "sign": "sign",
                "withdraw": "withdraw",
                "revoke_consent": "consent",
            }.get(action)
            if not scope:
                fail("UNKNOWN_ACTION", "Неизвестное действие")
            access(db, user, oid, scope)
        if action == "consent":
            if a["status"] not in {"DRAFT", "NEEDS_INFO"}:
                fail("INVALID_STATE", "Согласия фиксируются до подачи или в ответе на уточнение", 409)
            purpose = b.get("purpose")
            if purpose not in {"CREDIT_HISTORY", "DATA_PROCESSING"} or b.get("confirmed") is not True:
                fail("CONSENT_REQUIRED", "Выберите цель и явно подтвердите согласие")
            a["consents"].append(
                {
                    "id": str(uuid4()),
                    "purpose": purpose,
                    "subject": user.id,
                    "organization_id": oid,
                    "text_version": "DEMO_CONSENT_1",
                    "granted_at": c.data["now"],
                    "valid_until": month_shift(c.data["date"], 18),
                    "scope": "Синтетические данные выбранной организации",
                    "status": "ACTIVE",
                }
            )
        elif action == "revoke_consent":
            if a["status"] in {"SIGNED", "EXECUTING", "CONTRACT_ACTIVE"}:
                fail("CONTRACT_EXISTS", "После подписи правовые эффекты отзыва вне модели MVP", 409)
            for r in a["consents"]:
                if r["purpose"] == b.get("purpose"):
                    r["status"] = "REVOKED"
            invalidate_offer(a, c, "Отозвано основание для нового запроса")
            a["status"] = "WITHDRAWN"
        elif action == "submit":
            if a["status"] != "DRAFT":
                fail("INVALID_STATE", "Заявка уже отправлена или завершена", 409)
            for purpose in ("CREDIT_HISTORY", "DATA_PROCESSING"):
                if not any(
                    r["purpose"] == purpose and r["status"] == "ACTIVE" and r["valid_until"] >= c.data["date"] for r in a["consents"]
                ):
                    fail("CONSENT_REQUIRED", "До подачи нужны отдельные действующие согласия на обработку и кредитную историю")
            if not a["input"].get("financials_confirmed"):
                fail("FORECAST_CONFIRMATION_REQUIRED", "Подписант должен подтвердить финансовый прогноз и его допущения")
            for other in db.scalars(
                select(CreditRecord).where(CreditRecord.kind == "application", CreditRecord.organization_id == oid)
            ).all():
                if other.id != row.id and other.data["status"] not in TERMINAL | {"DRAFT"}:
                    fail(
                        "ACTIVE_APPLICATION_EXISTS",
                        "У организации уже есть незавершенная отправленная заявка: " + other.data["number"],
                        409,
                    )
            a["status"] = "SUBMITTED"
            a["phase"] = "VALIDATION"
            a["source_attempt"] = 0
            snapshot_application(a, c, user)
            event(a, "ApplicationSubmitted", user.name, c, version=len(a["versions"]))
            a["job_id"] = job(db, "sources", row.id, oid)
        elif action in {"save_reply", "respond"}:
            if a["status"] != "NEEDS_INFO":
                fail("INVALID_STATE", "Сейчас нет задания на уточнение", 409)
            message = str(b.get("message", "")).strip()
            if len(message) < 3:
                fail("REPLY_REQUIRED", "Опишите ответ на конкретный вопрос")
            incoming = safe_input(b.get("input") or a["input"])
            draft = {
                "input": {**a["input"], **{k: v for k, v in incoming.items() if k not in FORBIDDEN_INPUT}},
                "message": message,
                "author": user.id,
                "created_at": c.data["now"],
            }
            if action == "save_reply":
                a["reply_draft"] = draft
                for q in a["clarifications"]:
                    if q["status"] == "OPEN":
                        q["status"] = "DRAFT_REPLY"
            else:
                if not incoming.get("financials_confirmed"):
                    fail("FORECAST_CONFIRMATION_REQUIRED", "Подписант подтверждает обновленный прогноз")
                a["input"] = draft["input"]
                if a["scenario_id"] == "FIX-E":
                    # A replacement statement is required, not a magic text flag.
                    statement = next(
                        (
                            d
                            for d in reversed(a["documents"])
                            if d["kind"] == "statement" and d["status"] != "SUPERSEDED" and len(d.get("periods", [])) == 6
                        ),
                        None,
                    )
                    a["statement_completed"] = bool(statement)
                if a["scenario_id"] == "FIX-H":
                    if b.get("confirm_classification") is not True:
                        fail("RECONCILIATION_REQUIRED", "Явно подтвердите классификацию непокупательских поступлений")
                    a["reconciliation_confirmed"] = True
                    a["statement_classification"] = {
                        "raw_total_kopecks": 3000000000,
                        "own_transfers_kopecks": 300000000,
                        "new_credit_kopecks": 200000000,
                        "deposit_return_kopecks": 100000000,
                        "customer_receipts_kopecks": 2400000000,
                        "confirmed_by": user.id,
                        "message": message,
                    }
                for q in a["clarifications"]:
                    if q["status"] in {"OPEN", "DRAFT_REPLY"}:
                        q["replies"].append(draft)
                        q["status"] = "ANSWERED"
                a["status"] = "PROCESSING"
                a["source_attempt"] = 0
                snapshot_application(a, c, user)
                a["job_id"] = job(db, "sources", row.id, oid)
                event(a, "ClarificationAnswered", user.name, c, message=message)
        elif action == "claim":
            if a["status"] not in {"MANUAL_REVIEW", "NEEDS_INFO", "WAITING_SOURCE", "EXECUTING"}:
                fail("INVALID_STATE", "В этой стадии нет банковской задачи", 409)
            if a["assigned_analyst_id"] and a["assigned_analyst_id"] != user.id:
                fail("ALREADY_ASSIGNED", "Заявка уже назначена другому аналитику", 409)
            a["assigned_analyst_id"] = user.id
            a["assigned_analyst_name"] = user.name
        elif action == "release":
            assigned(user, a)
            if not str(b.get("reason", "")).strip():
                fail("REASON_REQUIRED", "Укажите причину освобождения задачи")
            a["assigned_analyst_id"] = None
            a["assigned_analyst_name"] = None
        elif action == "request_info":
            assigned(user, a)
            if a["status"] != "MANUAL_REVIEW":
                fail("INVALID_STATE", "Уточнение создается из ручного рассмотрения", 409)
            question = str(b.get("question", "")).strip()
            fields = b.get("fields", [])
            if len(question) < 10 or not fields:
                fail("SPECIFIC_QUESTION_REQUIRED", "Укажите конкретный вопрос и связанные поля/периоды")
            a["status"] = "NEEDS_INFO"
            clarification(a, c, question, fields, user.name, code="ANALYST_REQUEST_" + str(len(a["clarifications"]) + 1))
        elif action == "accept_document":
            assigned(user, a)
            reason = str(b.get("reason", "")).strip()
            if len(reason) < 10:
                fail("REASON_REQUIRED", "Зафиксируйте содержание проверки документа")
            document = next((d for d in a["documents"] if d["id"] == b.get("document_id")), None)
            if not document or document["status"] == "SUPERSEDED":
                fail("NOT_FOUND", "Действующая версия документа не найдена", 404)
            document.update(status="ACCEPTED_BY_ANALYST", accepted_by=user.id, accepted_reason=reason)
            if a["status"] == "MANUAL_REVIEW":
                a["status"] = "PROCESSING"
                a["job_id"] = job(db, "sources", row.id, oid)
        elif action == "decide":
            assigned(user, a)
            if a["status"] != "MANUAL_REVIEW":
                fail("INVALID_STATE", "Экономическое решение доступно только на ручном рассмотрении", 409)
            reason = str(b.get("reason", "")).strip()
            public = str(b.get("public_explanation", "")).strip()
            if len(reason) < 10 or len(public) < 10:
                fail("REASON_REQUIRED", "Укажите основание и отдельное понятное объяснение клиенту")
            evaluation_input = normalized(db, a, c)
            current = evaluate_normalized(evaluation_input)
            current.update(
                id=str(uuid4()),
                input_hash=digest(evaluation_input),
                input_snapshot=evaluation_input,
                created_at=c.data["now"],
                application_version=len(a["versions"]),
            )
            if b.get("outcome") == "DECLINE":
                if current["status"] in {"WAITING_SOURCE", "NEEDS_INFO", "NOT_ELIGIBLE"} or not check_fresh(a, c):
                    fail("MANDATORY_GATES", "Нельзя подменять техническое ожидание или неполноту экономическим отказом", 409)
                decision = {
                    "id": str(uuid4()),
                    "outcome": "DECLINE",
                    "reason": reason,
                    "public_explanation": public,
                    "actor": user.name,
                    "created_at": c.data["now"],
                    "policy_version": POLICY_VERSION,
                    "input_hash": current["input_hash"],
                    "application_version": len(a["versions"]),
                }
                a["decisions"].append(decision)
                a["status"] = "DECLINED"
                event(a, "DecisionRecorded", user.name, c, outcome="DECLINE", public_explanation=public)
            elif b.get("outcome") in {"APPROVE", "APPROVE_CHANGED"}:
                if not current.get("manual_approval_allowed"):
                    fail("MANDATORY_GATES", "Положительное решение запрещено: проверьте риск, источники и покрытие каждого платежа", 409)
                if b["outcome"] == "APPROVE":
                    calc = current.get("original") or {}
                    if not calc.get("passed"):
                        fail(
                            "MANDATORY_GATES",
                            "Исходный вариант не проходит финансовые правила. Можно выбрать только проверенную допустимую альтернативу",
                            409,
                        )
                else:
                    calc = next(
                        (x for x in current["alternatives"] if x.get("candidate_id") == b.get("candidate_id") and x["passed"]), None
                    )
                    if not calc:
                        fail("INVALID_CANDIDATE", "Выберите допустимую рассчитанную альтернативу", 422)
                current["offer"] = {
                    "product_code": a["input"]["product_code"],
                    "amount_kopecks": calc["amount_kopecks"],
                    "term_months": calc.get("months", 12),
                    "annual_rate_bps": 2400,
                    "fees_kopecks": 0,
                    "calculation": calc,
                    "unfunded_need_kopecks": calc.get("unfunded_need_kopecks", 0),
                    "change": calc.get("change"),
                    "notice": DISCLAIMER,
                }
                current["decision"] = b["outcome"]
                a["assessment"] = current
                publish_offer(db, a, c, current, user.name, public)
                a["decisions"][-1]["internal_reason"] = reason
                a["decisions"][-1]["hash"] = digest({k: v for k, v in a["decisions"][-1].items() if k != "hash"})
            else:
                fail("INVALID_OUTCOME", "Выберите допустимый результат решения")
        elif action in {"retry", "reopen"}:
            if action == "reopen":
                if a["status"] not in {"OFFER_READY", "OFFER_ACCEPTED"}:
                    fail("INVALID_STATE", "Можно пересмотреть только неподписанное предложение", 409)
            else:
                assigned(user, a)
                if a["status"] not in {"MANUAL_REVIEW", "WAITING_SOURCE", "EXECUTING"}:
                    fail("INVALID_STATE", "Нет задания для технического повтора", 409)
            if len(str(b.get("reason", "")).strip()) < 5:
                fail("REASON_REQUIRED", "Укажите причину технического повтора/пересмотра")
            if a["status"] == "EXECUTING":
                j = load(db, a["execution_job_id"], "job")
                jd = deepcopy(j.data)
                jd.update(status="PENDING", available_at=now().isoformat(), attempt=0)
                save(j, jd)
            else:
                invalidate_offer(a, c, b["reason"])
                a["status"] = "MANUAL_REVIEW" if action == "reopen" else "PROCESSING"
                if action == "reopen":
                    a["assigned_analyst_id"] = user.id
                    a["assigned_analyst_name"] = user.name
                else:
                    a["source_attempt"] = 0
                    a["job_id"] = job(db, "sources", row.id, oid)
        elif action == "note":
            a["internal_notes"].append(
                {"id": str(uuid4()), "actor": user.name, "message": str(b.get("message", ""))[:2000], "created_at": c.data["now"]}
            )
        elif action in {"accept", "decline", "sign"}:
            expected = "OFFER_ACCEPTED" if action == "sign" else "OFFER_READY"
            if a["status"] != expected:
                fail("INVALID_STATE", "Условия недоступны для этого действия. Откройте актуальную карточку", 409)
            if not confirm_offer(
                db, a, c, {**b, **({"offer_id": a["offer"]["id"], "offer_hash": a["offer"]["hash"]} if action == "sign" else {})}
            ):
                a["job_id"] = job(db, "sources", row.id, oid)
                save(row, a)
                result = public_app(row, user)
                result["notice"] = "Условия обновляются из-за изменившихся дат или оснований. Потребуется принять новую версию"
                return result
            if action == "accept":
                a["offer"]["status"] = "ACCEPTED"
                a["status"] = "OFFER_ACCEPTED"
                authority = access(db, user, oid, "accept")
                a["acceptance"] = {
                    "id": str(uuid4()),
                    "offer_id": a["offer"]["id"],
                    "offer_hash": a["offer"]["hash"],
                    "signer_id": user.id,
                    "authority_id": authority["id"],
                    "accepted_at": c.data["now"],
                    "sign_by": (datetime.fromisoformat(c.data["now"]) + timedelta(days=7)).isoformat(),
                    "status": "ACTIVE",
                }
                a["bundle"] = bundle_for(a, c)
                event(a, "OfferAccepted", user.name, c, offer_id=a["offer"]["id"])
            elif action == "decline":
                a["status"] = "OFFER_REJECTED"
                a["offer"]["status"] = "REJECTED"
                event(a, "OfferRejected", user.name, c, code="CLIENT_REJECTED")
            else:
                if b.get("bundle_hash") != a["bundle"]["hash"] or a["acceptance"]["status"] != "ACTIVE":
                    fail("BUNDLE_STALE", "Подпись относится к другому комплекту. Просмотрите актуальные документы", 409)
                authority = access(db, user, oid, "sign")
                a["signing"] = {
                    "id": str(uuid4()),
                    "subject": user.id,
                    "authority_id": authority["id"],
                    "acceptance_id": a["acceptance"]["id"],
                    "offer_id": a["offer"]["id"],
                    "bundle_hash": a["bundle"]["hash"],
                    "signed_at": c.data["now"],
                    "simulation": True,
                }
                a["status"] = "SIGNED"
                a["execution_job_id"] = job(db, "execute", row.id, oid)
                event(a, "SigningSimulated", user.name, c, bundle_hash=a["bundle"]["hash"])
        elif action == "withdraw":
            if a["status"] in TERMINAL | {"EXECUTING"}:
                fail("EXECUTION_STARTED", "Заявка завершена либо исполнение уже началось; отзыв недоступен", 409)
            if a["status"] == "SIGNED":
                j = load(db, a["execution_job_id"], "job")
                if j.data["status"] != "PENDING":
                    fail("EXECUTION_STARTED", "Исполнение уже началось", 409)
                jd = deepcopy(j.data)
                jd["status"] = "CANCELLED"
                save(j, jd)
            invalidate_offer(a, c, "Заявка отозвана клиентом")
            a["status"] = "WITHDRAWN"
        event(a, "Command:" + action, user.name, c)
        save(row, a)
        return public_app(row, user)

    return idempotent(db, user, request, "v2:command:" + application_id, b, run)


@router.get("/applications/{application_id}/bundle")
def get_bundle(application_id: str, user=Depends(get_user), db=Depends(get_db, scope="function")):
    row = owned(db, user, application_id)
    if not row.data.get("bundle"):
        fail("BUNDLE_NOT_READY", "Комплект появляется после принятия условий", 409)
    return row.data["bundle"]


from .credit_servicing import (
    CreditDomainError,
    facility_summary,
    validate_draw,
    validate_repayment_order,
    loan_repayment_quote,
    repay_loan_row,
    schedule_as_of,
)


def monetary(db, contract, oid, c, kind, amount, key, credit=False, **details):
    if type(amount) is not int or amount < 0:
        raise CreditDomainError("INVALID_MONEY", "Некорректная денежная операция")
    operation_id = hashlib.sha256(key.encode()).hexdigest()
    existing = db.get(CreditRecord, "op:" + operation_id)
    if existing:
        return existing.data
    account = load(db, "account:" + oid, "account")
    value = deepcopy(account.data)
    previous = value["balance_kopecks"]
    updated = previous + amount if credit else previous - amount
    if updated < 0:
        raise CreditDomainError("INSUFFICIENT_FUNDS", "Недостаточно средств на синтетическом счете")
    op = {
        "id": operation_id,
        "kind": kind,
        "amount_kopecks": amount,
        "direction": "CREDIT" if credit else "DEBIT",
        "before_balance_kopecks": previous,
        "after_balance_kopecks": updated,
        "contract_id": contract["id"],
        "occurred_at": c.data["now"],
        "recorded_at": now().isoformat(),
        "synthetic": True,
        **details,
    }
    value["balance_kopecks"] = updated
    value["operations"].append(op)
    save(account, value)
    contract.setdefault("operations", []).append(op)
    db.add(CreditRecord(id="op:" + operation_id, kind="monetary_operation", organization_id=oid, revision=1, data=op))
    return op


def schedule_receipts(contract, plan):
    for e in plan["events"]:
        if e["kind"] != "TARGET_PAYMENT":
            contract.setdefault("cash_events", []).append({**deepcopy(e), "executed": False})


def execute_contract(db, row, c):
    a = deepcopy(row.data)
    if a["status"] == "CONTRACT_ACTIVE":
        return
    if a["status"] != "EXECUTING" or not a.get("signing"):
        return
    offer = a["offer"]
    if offer["planned_on"] != c.data["date"] or a["signing"]["bundle_hash"] != a["bundle"]["hash"]:
        raise CreditDomainError("EXECUTION_DATE_CHANGED", "Дата или подписанный комплект не совпадают с исполнением")
    # One contract is keyed by one application; retries cannot create another.
    cid = a.get("contract_id") or "contract:" + row.id
    existing = db.get(CreditRecord, cid)
    if existing:
        a["contract_id"] = cid
        a["status"] = "CONTRACT_ACTIVE"
        save(row, a)
        return
    org = load(db, row.organization_id).data
    if offer["product_code"] == "DEMO_TERM_LOAN_V1":
        known = organization_cash_sources(db, row.organization_id)
        for candidate in monthly_cash_events(a["assessment"]["input_snapshot"]["forecast"][:offer["term_months"]], c.data["date"], org):
            prior = known.get(candidate["cash_source"])
            if prior and prior["signature"] != tuple(candidate[k] for k in ("date", "kind", "amount_kopecks")):
                raise CreditDomainError("CASHFLOW_CONFLICT", "Общий прогноз изменился; выдача требует уточнения")
    cd = {
        "id": cid,
        "application_id": row.id,
        "organization_id": row.organization_id,
        "organization_name": a["organization_name"],
        "product_code": offer["product_code"],
        "status": "ACTIVE",
        "issued_on": c.data["date"],
        "offer": deepcopy(offer),
        "signing": deepcopy(a["signing"]),
        "history": [],
        "operations": [],
        "tranches": [],
        "cash_events": [],
        "checks": deepcopy(a["checks"]),
        "scenario_id": a["scenario_id"],
        "original_cycle_plan": deepcopy(a["input"].get("cycle_plan", {})),
        "scheduled": None,
        "facility": None,
    }
    if offer["product_code"] == "DEMO_TERM_LOAN_V1":
        cd["scheduled"] = annuity_schedule(offer["amount_kopecks"], offer["term_months"], c.data["date"])
        cd["principal_outstanding_kopecks"] = offer["amount_kopecks"]
        monetary(db, cd, row.organization_id, c, "LOAN_DISBURSEMENT", offer["amount_kopecks"], cid + ":initial", credit=True)
        target = min(offer["amount_kopecks"] + a["input"]["need"]["own_contribution_kopecks"], a["input"]["need"]["total_cost_kopecks"])
        monetary(db, cd, row.organization_id, c, "TARGET_PURCHASE", target, cid + ":target")
        # The exact confirmed flows are dated on each payment interval end.
        # New loan proceeds and target purchase never enter operating CFADS.
        org = load(db, row.organization_id).data
        for e in monthly_cash_events(a["assessment"]["input_snapshot"]["forecast"][:offer["term_months"]], c.data["date"], org):
            cd["cash_events"].append({**e, "id": cid + ":" + e["cash_source"]})
        event(cd, "ExecutionCompleted", "SYSTEM", c, amount_kopecks=offer["amount_kopecks"])
    else:
        cd["facility"] = {
            "limit_kopecks": offer["amount_kopecks"],
            "expires_on": month_shift(c.data["date"], 12),
            "status": "ACTIVE",
            "control_clear": True,
        }
        cd["principal_outstanding_kopecks"] = 0
        # Activation deliberately has no money operation.
        event(cd, "FacilityActivated", "SYSTEM", c, limit_kopecks=offer["amount_kopecks"])
    db.add(CreditRecord(id=cid, kind="contract", organization_id=row.organization_id, revision=1, data=cd))
    a["status"] = "CONTRACT_ACTIVE"
    a["contract_id"] = cid
    a["phase"] = None
    event(a, "ExecutionCompleted", "SYSTEM", c, contract_id=cid)
    save(row, a)
    org = load(db, row.organization_id)
    od = deepcopy(org.data)
    od["obligations_revision"] += 1
    save(org, od)


def contract_view(db, row, c):
    value = data(row)
    value["account"] = data(load(db, "account:" + row.organization_id))
    if value.get("scheduled"):
        calculated = schedule_as_of(value["scheduled"], c.data["date"])
        value["schedule"] = [
            {**r, "due_date": r["due_on"], "total_kopecks": r["payment_kopecks"], "balance_kopecks": r["remaining_principal_kopecks"]}
            for r in calculated["rows"]
        ]
    else:
        value["schedule"] = []
    if value.get("facility"):
        summary = facility_summary(value["facility"], value["tranches"], c.data["date"])
        value["facility"] = {**value["facility"], **summary, "drawn_kopecks": summary["principal_kopecks"]}
        public = []
        for tr in value["tranches"]:
            q = (
                tranche_quote(tr, c.data["date"])
                if tr["status"] in {"ACTIVE", "OVERDUE", "CLOSED", "REPAID"}
                else {"interest_kopecks": 0, "total_kopecks": 0}
            )
            public.append(
                {
                    **tr,
                    "principal_outstanding_kopecks": tr.get("principal_kopecks", 0),
                    "disbursed_on": tr.get("issued_on"),
                    "due_on": tr.get("matures_on"),
                    "interest_kopecks": q["interest_kopecks"],
                    "total_due_kopecks": q["total_kopecks"],
                    "status": "REPAID" if tr["status"] == "CLOSED" else tr["status"],
                }
            )
        value["tranches"] = public
        value["principal_outstanding_kopecks"] = summary["principal_kopecks"]
        if summary["status"] == "CLOSED":
            value["status"] = "CLOSED"
    value.pop("scenario_id", None)
    return value


@router.get("/contracts/{contract_id}")
def get_contract(contract_id: str, user=Depends(get_user), db=Depends(get_db, scope="function")):
    return contract_view(db, owned(db, user, contract_id, "contract"), clock(db))


def contract_checks_fresh(cd, c):
    return check_fresh(cd, c)


def draw_plan(db, cd, c, b):
    amount = b.get("amount_kopecks")
    days = b.get("days")
    allowed = validate_draw(cd["facility"], cd["tranches"], amount, days, c.data["date"], checks_fresh=contract_checks_fresh(cd, c))
    plan = deepcopy(b.get("cycle_plan") or {})
    if not plan.get("confirmed"):
        fail("CYCLE_CONFIRMATION_REQUIRED", "Подтвердите календарь текущего цикла")
    safe_input({"product_code": "DEMO_REVOLVING_V1", "amount_kopecks": amount, "term_months": 12, "cycle_plan": plan})
    balance = load(db, "account:" + cd["organization_id"]).data["balance_kopecks"]
    if type(plan.get("initial_cash_kopecks")) is not int or plan["initial_cash_kopecks"] > balance:
        fail("UNCONFIRMED_RESERVE", "План обещает больше свободных денег, чем есть на синтетическом счете")
    plan = shared_cycle_plan(db, cd["organization_id"], plan, c, days)
    calc = evaluate_cycle(evaluation_cycle_plan(plan), amount, days, c.data["date"])
    if not calc["passed"]:
        fail(
            "CYCLE_CASHFLOW_INSUFFICIENT",
            "Календарь не покрывает возврат тела и процентов: " + ", ".join(str(r) for r in calc.get("reasons", [])),
        )
    return allowed, plan, calc


def make_quote(db, cd, row, c, b, action):
    if action == "draw_preview":
        if not cd.get("facility"):
            fail("WRONG_MECHANISM", "Транши доступны только по линии")
        allowed, plan, calculation = draw_plan(db, cd, c, b)
        fields = {
            "kind": "draw",
            "amount_kopecks": allowed["amount_kopecks"],
            "principal_kopecks": allowed["amount_kopecks"],
            "interest_kopecks": calculation["interest_kopecks"],
            "days": allowed["days"],
            "due_on": allowed["matures_on"],
            "cycle_plan": plan,
            "calculation": calculation,
        }
    elif action == "pay_preview":
        if not cd.get("scheduled"):
            fail("WRONG_MECHANISM", "Выберите транш для погашения линии")
        q = loan_repayment_quote(cd["scheduled"], c.data["date"])
        if b.get("row_number", q["row_number"]) != q["row_number"]:
            fail("EARLIEST_PAYMENT_FIRST", "Оплатите наиболее ранний наступивший платеж")
        fields = {"kind": "pay", "amount_kopecks": q["total_kopecks"], **q}
    else:
        if not cd.get("facility"):
            fail("WRONG_MECHANISM", "Для разового кредита оплачивается строка графика")
        tr = next((t for t in cd["tranches"] if t["id"] == b.get("tranche_id")), None)
        if not tr:
            fail("NOT_FOUND", "Транш не найден", 404)
        validate_repayment_order(tr["id"], cd["facility"], cd["tranches"], c.data["date"])
        q = tranche_quote(tr, c.data["date"])
        amount = q["total_kopecks"] if b.get("full") else b.get("amount_kopecks")
        # Validate same-day, amount, balance before returning a confirmable quote.
        distribution = repay_tranche(tr, amount, c.data["date"], load(db, "account:" + cd["organization_id"]).data["balance_kopecks"])
        fields = {
            "kind": "repay",
            "tranche_id": tr["id"],
            "amount_kopecks": amount,
            **distribution["payment"],
            "full_debt_kopecks": q["total_kopecks"],
        }
    quote = {
        "id": str(uuid4()),
        "contract_id": row.id,
        "contract_revision": row.revision,
        "clock_revision": c.revision,
        "on_date": c.data["date"],
        "expires_at": (now() + timedelta(minutes=5)).isoformat(),
        **fields,
    }
    quote["hash"] = digest(quote)
    db.add(CreditRecord(id=quote["id"], kind="quote", organization_id=row.organization_id, revision=1, data=quote))
    return {"quote": quote}


@router.post("/contracts/{contract_id}/commands")
def contract_commands(contract_id: str, body: Command, request: Request, user=Depends(get_user), db=Depends(get_db, scope="function")):
    b = body.model_dump()
    action = b["action"]

    def run(c):
        row = owned(db, user, contract_id, "contract", action="draw" if action.startswith("draw") else "repay")
        revision(row, body.expected_version, request)
        cd = deepcopy(row.data)
        if action == "refresh_checks":
            if not cd.get("facility"):
                fail("WRONG_MECHANISM", "Обновление проверок предназначено для новых траншей")
            approw = load(db, cd["application_id"])
            a = deepcopy(approw.data)
            if not any(
                r["purpose"] == "CREDIT_HISTORY" and r["status"] == "ACTIVE" and r["valid_until"] >= c.data["date"] for r in a["consents"]
            ):
                fail("CONSENT_REQUIRED", "Нужно действующее согласие на кредитную историю")
            for name in ("registry", "credit_history", "control"):
                cd["checks"].append(
                    {
                        "id": str(uuid4()),
                        "source": name,
                        "input_hash": digest({"organization_id": row.organization_id, "source": name}),
                        "fetched_at": c.data["now"],
                        "valid_until": (datetime.fromisoformat(c.data["now"]) + timedelta(days=7)).isoformat(),
                        "status": "SUCCEEDED",
                        "result": "CLEAR",
                        "attempt": 1,
                        "synthetic": True,
                    }
                )
            cd["facility"]["control_clear"] = True
            event(cd, "CheckCompleted", user.name, c, reason="Обновление синтетических источников перед траншем")
            save(row, cd)
            return contract_view(db, row, c)
        if action in {"draw_preview", "pay_preview", "repay_preview"}:
            return make_quote(db, cd, row, c, b, action)
        if action not in {"draw", "pay", "repay"}:
            fail("UNKNOWN_ACTION", "Неизвестная команда сопровождения")
        if b.get("confirmed") is not True:
            fail("CONFIRMATION_REQUIRED", "Нужно отдельное подтверждение финансовой операции")
        quote_row = owned(db, user, b.get("quote_id", ""), "quote")
        q = quote_row.data
        if (
            q["contract_id"] != row.id
            or q["kind"] != action
            or q["contract_revision"] != row.revision
            or q["clock_revision"] != c.revision
            or q["expires_at"] <= now().isoformat()
        ):
            fail("QUOTE_STALE", "Расчет устарел. Пересчитайте и подтвердите сумму заново", 409)
        if action == "draw":
            allowed, plan, calculation = draw_plan(db, cd, c, q)
            # Reservation is persisted before the worker can disburse it.
            tr = {
                "id": str(uuid4()),
                "amount_kopecks": allowed["amount_kopecks"],
                "principal_kopecks": 0,
                "issued_on": c.data["date"],
                "matures_on": allowed["matures_on"],
                "interest_anchor_on": c.data["date"],
                "accrued_unrounded_kopecks": "0",
                "annual_rate_bps": 2400,
                "status": "RESERVED",
                "cycle_plan": plan,
                "calculation": calculation,
            }
            tr["execution_job_id"] = job(db, "draw", row.id, row.organization_id, tranche_id=tr["id"])
            cd["tranches"].append(tr)
            event(cd, "TrancheReserved", user.name, c, tranche_id=tr["id"], amount_kopecks=tr["amount_kopecks"])
        else:
            account = load(db, "account:" + row.organization_id).data
            if action == "pay":
                outcome = repay_loan_row(cd["scheduled"], q["row_number"], q["amount_kopecks"], c.data["date"], account["balance_kopecks"])
                cd["scheduled"] = outcome["schedule"]
                cd["principal_outstanding_kopecks"] = outcome["remaining_principal_kopecks"]
                cd["status"] = outcome["status"]
                distribution = {
                    "principal_kopecks": q["principal_kopecks"],
                    "interest_kopecks": q["interest_kopecks"],
                    "row_number": q["row_number"],
                }
            else:
                index = next(i for i, t in enumerate(cd["tranches"]) if t["id"] == q["tranche_id"])
                validate_repayment_order(q["tranche_id"], cd["facility"], cd["tranches"], c.data["date"])
                outcome = repay_tranche(cd["tranches"][index], q["amount_kopecks"], c.data["date"], account["balance_kopecks"])
                cd["tranches"][index] = outcome["tranche"]
                distribution = outcome["payment"]
                previous_status = cd["facility"]["status"]
                if previous_status == "BLOCKED" and not contract_checks_fresh(cd, c):
                    cd["facility"]["control_clear"] = False
                summary = facility_summary(cd["facility"], cd["tranches"], c.data["date"])
                cd["facility"]["status"] = summary["status"]
                if summary["status"] == "CLOSED":
                    cd["status"] = "CLOSED"
                if previous_status != summary["status"]:
                    event(cd, "Facility" + summary["status"].title(), "SYSTEM", c)
            monetary(db, cd, row.organization_id, c, "REPAYMENT", q["amount_kopecks"], "repayment:" + q["id"], **distribution)
            event(cd, "RepaymentPosted", user.name, c, amount_kopecks=q["amount_kopecks"], **distribution)
            org = load(db, row.organization_id)
            od = deepcopy(org.data)
            od["obligations_revision"] += 1
            save(org, od)
        save(row, cd)
        return contract_view(db, row, c)

    try:
        return idempotent(db, user, request, "v2:servicing:" + contract_id, b, run)
    except CreditDomainError as exc:
        fail(exc.code, str(exc))


def execute_draw(db, row, c, tranche_id):
    cd = deepcopy(row.data)
    tr = next(t for t in cd["tranches"] if t["id"] == tranche_id)
    if tr["status"] not in {"RESERVED", "DISBURSING"}:
        return
    # Validate again immediately before crediting, excluding this reservation.
    others = [t for t in cd["tranches"] if t["id"] != tranche_id]
    validate_draw(
        cd["facility"],
        others,
        tr["amount_kopecks"],
        (date.fromisoformat(tr["matures_on"]) - date.fromisoformat(tr["issued_on"])).days,
        c.data["date"],
        checks_fresh=contract_checks_fresh(cd, c),
    )
    if tr["issued_on"] != c.data["date"]:
        raise CreditDomainError("EXECUTION_DATE_CHANGED", "Дата резерва изменилась; выдача задним числом запрещена")
    tr["status"] = "ACTIVE"
    tr["principal_kopecks"] = tr["amount_kopecks"]
    monetary(
        db,
        cd,
        row.organization_id,
        c,
        "TRANCHE_DISBURSEMENT",
        tr["amount_kopecks"],
        "tranche:" + tr["id"],
        credit=True,
        tranche_id=tr["id"],
    )
    for e in tr["cycle_plan"]["events"]:
        if e["kind"] == "TARGET_PAYMENT":
            monetary(
                db,
                cd,
                row.organization_id,
                c,
                "TARGET_PURCHASE",
                e["amount_kopecks"],
                "target:" + tr["id"] + ":" + e["id"],
                tranche_id=tr["id"],
            )
        else:
            # Shared receipts with the same dated source ID are executed once.
            source = e["id"]
            if not any(x.get("source_event_id") == source for x in cd["cash_events"]):
                cd["cash_events"].append({**deepcopy(e), "id": tr["id"] + ":" + source, "source_event_id": source, "executed": False})
    event(cd, "TrancheDisbursed", "SYSTEM", c, tranche_id=tr["id"], amount_kopecks=tr["amount_kopecks"])
    save(row, cd)
    org = load(db, row.organization_id)
    od = deepcopy(org.data)
    od["obligations_revision"] += 1
    save(org, od)


def apply_date(db, c):
    for row in db.scalars(select(CreditRecord).where(CreditRecord.kind == "contract").order_by(CreditRecord.id)).all():
        cd = deepcopy(row.data)
        if cd["status"] == "CLOSED":
            continue
        # Receipts precede same-day scheduled outflows. The application explicitly
        # models term liquidity monthly; line assessment checks each event date.
        due = sorted(
            [e for e in cd["cash_events"] if not e["executed"] and e["date"] <= c.data["date"]],
            key=lambda e: (e["date"], 0 if e["kind"] == "CUSTOMER_RECEIPT" else 1, e["id"]),
        )
        for e in due:
            try:
                identity = cash_source(e, cd)
                prior = organization_cash_sources(db, row.organization_id).get(identity)
                if prior and prior["executed"]:
                    e["executed"] = True
                    continue
                if not e["amount_kopecks"]:
                    e["executed"] = True
                    continue
                monetary(
                    db,
                    cd,
                    row.organization_id,
                    c,
                    e["kind"],
                    e["amount_kopecks"],
                    "cash:" + row.organization_id + ":" + identity,
                    credit=e["kind"] == "CUSTOMER_RECEIPT",
                    source_event_id=e["id"],
                )
                e["executed"] = True
            except CreditDomainError:
                # Never fabricate funds or silently mark an unpaid expense paid.
                e["error"] = "INSUFFICIENT_FUNDS"
                event(cd, "CashEventPending", "SYSTEM", c, event_id=e["id"])
        if cd.get("scheduled"):
            previous = {r["number"]: r["status"] for r in cd["scheduled"]["rows"]}
            cd["scheduled"] = schedule_as_of(cd["scheduled"], c.data["date"])
            for r in cd["scheduled"]["rows"]:
                if r["status"] == "OVERDUE" and previous[r["number"]] != "OVERDUE":
                    event(cd, "DelinquencyDetected", "SYSTEM", c, row_number=r["number"])
        if cd.get("facility"):
            for tr in cd["tranches"]:
                if tr["status"] == "ACTIVE" and tr["matures_on"] < c.data["date"]:
                    tr["status"] = "OVERDUE"
                    event(cd, "DelinquencyDetected", "SYSTEM", c, tranche_id=tr["id"])
            previous = cd["facility"]["status"]
            summary = facility_summary(cd["facility"], cd["tranches"], c.data["date"])
            cd["facility"]["status"] = summary["status"]
            if previous != summary["status"]:
                event(cd, "Facility" + summary["status"].title(), "SYSTEM", c)
            if summary["status"] == "CLOSED":
                cd["status"] = "CLOSED"
        save(row, cd)
    for row in db.scalars(select(CreditRecord).where(CreditRecord.kind == "application")).all():
        a = deepcopy(row.data)
        changed = False
        if a["status"] == "OFFER_READY" and a["offer"]["expires_at"] <= c.data["now"]:
            a["status"] = "OFFER_EXPIRED"
            a["offer"]["status"] = "EXPIRED"
            changed = True
            event(a, "OfferExpired", "SYSTEM", c)
        elif a["status"] == "OFFER_ACCEPTED" and a["acceptance"]["sign_by"] <= c.data["now"]:
            invalidate_offer(a, c, "Истек отдельный срок подписи: нужно обновить основания и принять условия заново")
            a["status"] = "PROCESSING"
            a["source_attempt"] = 0
            a["job_id"] = job(db, "sources", row.id, row.organization_id)
            changed = True
        if changed:
            save(row, a)


@router.post("/admin/commands")
def admin_commands(body: Command, request: Request, user=Depends(get_user), db=Depends(get_db, scope="function")):
    if user.role != "demo_admin":
        fail("ACCESS_DENIED", "Только демооператор управляет временем и сценариями", 403)
    b = body.model_dump()

    def run(c):
        revision(c, body.expected_version, request)
        if b["action"] == "advance":
            days = b.get("days")
            if type(days) is not int or not 1 <= days <= 31:
                fail("INVALID_ADVANCE", "Можно продвинуть время вперед на 1–31 день")
            records = db.scalars(select(CreditRecord).where(CreditRecord.kind.in_(["application", "contract", "job"]))).all()
            for row in records:
                if row.kind == "application" and row.data["status"] in {"SIGNED", "EXECUTING"}:
                    fail("PENDING_FINANCIAL_EXECUTION", "Сначала завершите исполнение подписанного договора", 409)
                if row.kind == "contract" and any(t["status"] in {"RESERVED", "DISBURSING"} for t in row.data["tranches"]):
                    fail("PENDING_FINANCIAL_EXECUTION", "Сначала завершите выдачу или освобождение резерва транша", 409)
            value = deepcopy(c.data)
            for _ in range(days):
                instant = datetime.fromisoformat(value["now"]) + timedelta(days=1)
                value.update(now=instant.isoformat(), date=instant.date().isoformat())
                c.data = deepcopy(value)
                apply_date(db, c)
            event(value, "DemoClockAdvanced", user.name, c, days=days)
            save(c, value)
        elif b["action"] == "bind_scenario":
            row = load(db, b.get("application_id", ""), "application")
            a = deepcopy(row.data)
            if a["status"] != "DRAFT":
                fail("SCENARIO_LOCKED", "Сценарий нельзя менять после подачи", 409)
            scenario = b.get("scenario_id")
            permitted = {"FIX-" + x for x in PROFILE_LABELS} | {"SOURCE_ALWAYS_TIMEOUT", "EXECUTION_FAIL_ONCE", "TRANCHE_FAIL_FINAL"}
            if scenario not in permitted:
                fail("INVALID_SCENARIO", "Выберите опубликованный синтетический сценарий")
            # Scenario affects mock behavior only, never rewrites the borrower's
            # financial facts or product behind their back.
            a["scenario_id"] = scenario
            event(a, "ScenarioBound", user.name, c, scenario_id=scenario)
            save(row, a)
            value = deepcopy(c.data)
            event(value, "ScenarioBound", user.name, c, application_id=row.id, scenario_id=scenario)
            save(c, value)
        else:
            fail("UNKNOWN_ACTION", "Неизвестная команда демооператора")
        return {"clock": data(c), "notice": "Демонстрационная команда выполнена"}

    return idempotent(db, user, request, "v2:admin", b, run)


@router.post("/applications/{application_id}/documents", status_code=201)
async def upload_document(
    application_id: str,
    request: Request,
    file: UploadFile = File(...),
    kind: str = Form(...),
    expected_version: int = Form(...),
    replaces_id: str | None = Form(None),
    user=Depends(get_user),
    db=Depends(get_db, scope="function"),
):
    content = await file.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        fail("FILE_TOO_LARGE", "Максимальный размер файла — 10 МиБ", 413)
    if not content:
        fail("EMPTY_FILE", "Файл пуст")
    filename = Path(file.filename or "document").name
    extension = Path(filename).suffix.lower()
    allowed = {
        ".pdf": ("application/pdf", b"%PDF-"),
        ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
        ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
        ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
        ".csv": ("text/csv", None),
    }
    if extension not in allowed or len(filename) > 180 or any(ord(ch) < 32 for ch in filename):
        fail("FILE_TYPE", "Разрешены PDF, PNG, JPEG и CSV; имя до 180 символов")
    media, signature = allowed[extension]
    if (
        file.content_type not in {media, "application/octet-stream", "application/vnd.ms-excel" if extension == ".csv" else media}
        or signature
        and not content.startswith(signature)
    ):
        fail("FILE_SIGNATURE", "MIME, расширение и содержимое файла не совпадают")
    periods = []
    parsed = []
    if extension == ".csv":
        try:
            decoded = content.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(decoded))
            columns = {"synthetic_id", "document_kind", "period", "customer_receipts_kopecks", "operating_payments_kopecks"}
            if not reader.fieldnames or set(reader.fieldnames) != columns:
                fail(
                    "CSV_HEADER",
                    "CSV: нужны колонки synthetic_id,document_kind,period,customer_receipts_kopecks,operating_payments_kopecks",
                )
            for number, item in enumerate(reader, 2):
                if number > 502:
                    fail("CSV_ROWS", "CSV содержит слишком много строк")
                try:
                    date.fromisoformat(item["period"] + "-01")
                    for key in ("customer_receipts_kopecks", "operating_payments_kopecks"):
                        if not item[key].isdigit() or int(item[key]) > 10000000000000:
                            raise ValueError()
                    parsed.append(item)
                    periods.append(item["period"])
                except (ValueError, TypeError, KeyError):
                    fail("CSV_ROW", f"CSV: проверьте период и целые суммы в строке {number}")
            if len(set(periods)) != len(periods):
                fail("CSV_PERIOD_DUPLICATE", "CSV: период не должен повторяться")
        except UnicodeDecodeError:
            fail("CSV_ENCODING", "CSV должен быть в UTF-8")
    payload = {
        "expected_version": expected_version,
        "kind": kind,
        "replaces_id": replaces_id,
        "name": filename,
        "hash": hashlib.sha256(content).hexdigest(),
    }

    def run(c):
        row = owned(db, user, application_id, action="prepare")
        revision(row, expected_version, request)
        a = deepcopy(row.data)
        if a["status"] not in {"DRAFT", "NEEDS_INFO"}:
            fail("INVALID_STATE", "Файлы добавляются в черновике или через уточнение", 409)
        if kind not in {"statement", "financial_plan", "purpose", "annual_report", "authority"}:
            fail("DOCUMENT_KIND", "Укажите назначение файла")
        if len(a["documents"]) >= 20:
            fail("DOCUMENT_LIMIT", "На заявку разрешено до 20 версий файлов")
        org = load(db, row.organization_id).data
        if parsed and any(item["synthetic_id"] != org["synthetic_id"] or item["document_kind"] != kind for item in parsed):
            fail("DOCUMENT_OWNER", "CSV относится к другой организации или назначению документа")
        previous = (
            next((d for d in a["documents"] if d["id"] == replaces_id), None)
            if replaces_id
            else next((d for d in reversed(a["documents"]) if d["kind"] == kind and d["status"] != "SUPERSEDED"), None)
        )
        if previous:
            previous["status"] = "SUPERSEDED"
        # The manifest verifies exact structured values against the published
        # synthetic financial inputs. Arbitrary PDF/CSV still needs an analyst.
        published = profile_input(org["synthetic_id"][-1], c.data["date"])["history"]
        expected = {p["period"]: (p["customer_receipts_kopecks"], p["operating_payments_kopecks"]) for p in published}
        matched = (
            bool(parsed)
            and len(parsed) == 6
            and all(
                expected.get(item["period"]) == (int(item["customer_receipts_kopecks"]), int(item["operating_payments_kopecks"]))
                for item in parsed
            )
        )
        document = {
            "id": str(uuid4()),
            "kind": kind,
            "name": filename,
            "media_type": media,
            "size": len(content),
            "hash": payload["hash"],
            "periods": periods,
            "version": 1 + max((d["version"] for d in a["documents"] if d["kind"] == kind), default=0),
            "replaces_id": previous["id"] if previous else None,
            "structured_history": [
                {
                    "period": item["period"],
                    "customer_receipts_kopecks": int(item["customer_receipts_kopecks"]),
                    "operating_payments_kopecks": int(item["operating_payments_kopecks"]),
                }
                for item in parsed
            ],
            "status": "VALIDATED" if matched else "UPLOADED",
            "source": "PublishedSyntheticManifest" if matched else "ClientFileNeedsAnalyst",
            "created_at": c.data["now"],
        }
        a["documents"].append(document)
        db.add(CreditBlob(id=document["id"], application_id=row.id, content=content))
        event(a, "DocumentVersionCreated", user.name, c, document_id=document["id"], kind=kind, hash=document["hash"])
        save(row, a)
        return public_app(row, user)

    return idempotent(db, user, request, "v2:upload:" + application_id, payload, run)


@router.get("/documents/{document_id}/content")
def download_document(document_id: str, user=Depends(get_user), db=Depends(get_db, scope="function")):
    blob = db.get(CreditBlob, document_id)
    if not blob:
        fail("NOT_FOUND", "Документ не найден", 404)
    row = owned(db, user, blob.application_id)
    doc = next(d for d in row.data["documents"] if d["id"] == document_id)
    from urllib.parse import quote

    return Response(
        content=blob.content,
        media_type=doc["media_type"],
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(doc["name"]), "X-Content-Type-Options": "nosniff"},
    )


@router.get("/applications/{application_id}/document-template")
def document_template(application_id: str, kind: str = "statement", user=Depends(get_user), db=Depends(get_db, scope="function")):
    row = owned(db, user, application_id)
    org = load(db, row.organization_id).data
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["synthetic_id", "document_kind", "period", "customer_receipts_kopecks", "operating_payments_kopecks"])
    for p in row.data["input"]["history"]:
        writer.writerow([org["synthetic_id"], kind, p["period"], p["customer_receipts_kopecks"], p["operating_payments_kopecks"]])
    return Response(
        output.getvalue().encode(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="synthetic-template.csv"'}
    )


def process_due_jobs():
    """One transactional worker tick; committed jobs survive API/worker restart.

    A database lock is the claim/lease: a crashed process releases its uncommitted
    claim and retries the same persisted logical job. No network work is performed
    while holding this lock, only deterministic local adapters.
    """
    count = 0
    with SessionLocal() as db:
        if SQLITE:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        c = clock(db, True)
        if not c:
            return 0
        pending = db.scalars(
            select(CreditRecord).where(CreditRecord.kind == "job").order_by(CreditRecord.created_at, CreditRecord.id)
        ).all()
        for row in pending:
            j = deepcopy(row.data)
            if j["status"] != "PENDING" or j["available_at"] > now().isoformat():
                continue
            target = load(db, j["target_id"])
            if target.kind == "application" and target.data["status"] in TERMINAL | {"WITHDRAWN"}:
                j["status"] = "CANCELLED"
                save(row, j)
                continue
            j["attempt"] += 1
            try:
                with db.begin_nested():
                    if j["kind"] == "sources":
                        done = pipeline(db, target, c)
                    elif j["kind"] == "execute":
                        if target.data["status"] == "SIGNED":
                            a = deepcopy(target.data)
                            a["status"] = "EXECUTING"
                            event(a, "ExecutionStarted", "SYSTEM", c)
                            save(target, a)
                            j["attempt"] -= 1
                            done = False
                        else:
                            if target.data["scenario_id"] == "EXECUTION_FAIL_ONCE" and j["attempt"] == 1:
                                raise CreditDomainError(
                                    "SYNTHETIC_EXECUTION_TIMEOUT", "Демонстрационный технический сбой: повтор того же задания"
                                )
                            execute_contract(db, target, c)
                            done = True
                    elif j["kind"] == "draw":
                        if target.data["scenario_id"] == "TRANCHE_FAIL_FINAL":
                            raise CreditDomainError("SYNTHETIC_DRAW_TIMEOUT", "Демонстрационная ошибка выдачи транша")
                        execute_draw(db, target, c, j["tranche_id"])
                        done = True
                    else:
                        done = True
                j["status"] = "SUCCEEDED" if done else "PENDING"
                if not done:
                    delay = [2, 5, 10][min(max(j["attempt"] - 1, 0), 2)] if j["kind"] == "sources" else 0.3
                    j["available_at"] = (now() + timedelta(seconds=delay)).isoformat()
                j["history"].append({"attempt": j["attempt"], "recorded_at": now().isoformat(), "status": j["status"]})
            except (CreditDomainError, ValueError, KeyError) as exc:
                j["error"] = {"code": getattr(exc, "code", "EXECUTION_ERROR"), "message": str(exc)}
                j["history"].append({"attempt": j["attempt"], "recorded_at": now().isoformat(), "error": j["error"]})
                if j["attempt"] >= 4:
                    j["status"] = "FAILED_FINAL"
                    if j["kind"] == "draw":
                        cd = deepcopy(target.data)
                        tr = next(t for t in cd["tranches"] if t["id"] == j["tranche_id"])
                        tr["status"] = "FAILED"
                        event(cd, "TrancheFailed", "SYSTEM", c, tranche_id=tr["id"], reason=j["error"]["message"])
                        save(target, cd)
                else:
                    j["available_at"] = (now() + timedelta(seconds=[2, 5, 10][j["attempt"] - 1])).isoformat()
            save(row, j)
            count += 1
        db.commit()
    return count


async def worker_loop():
    import logging

    while True:
        try:
            await asyncio.to_thread(process_due_jobs)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger("credit.v2").exception("Credit-cycle worker tick failed; persisted jobs will retry")
        await asyncio.sleep(0.5)
