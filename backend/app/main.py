import hashlib
import json
import logging
import os
import secrets
import tempfile
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
from fastapi import FastAPI, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text, case
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from .db import BACKEND_ROOT, SQLITE, SessionLocal, engine
from .models import Application, Audit, Company, Document, Idempotency, LoginAttempt, Note, Notification, Session, User, now
from .security import COOKIE_NAME, get_db, get_user, require_bank, require_entrepreneur, password_hash, verify_password, utc, user_dict
from .schemas import Login, CompanyEdit, Calculation, ApplicationCreate, ApplicationEdit, Action, NoteCreate
from .lending import PRODUCTS, ATTENTION_STATUSES, BANK_ACTIONS, ENTREPRENEUR_TRANSITIONS, calculate, money, snapshot, transition, process

logger = logging.getLogger("credit")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
DUMMY_HASH = password_hash("not-an-account-password")


@asynccontextmanager
async def lifespan(_):
    from alembic import command
    from alembic.config import Config
    from .seed import seed
    from .v2_seed import seed_v2
    from .v2 import worker_loop

    upload_tmp = BACKEND_ROOT / "work" / "uploads"
    upload_tmp.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(upload_tmp)
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    if os.getenv("SEED_DEMO", "true").lower() == "true":
        with SessionLocal() as db:
            seed(db)
            seed_v2(db)
            db.commit()
    worker = asyncio.create_task(worker_loop()) if os.getenv("V2_WORKER_ENABLED", "true").lower() == "true" else None
    try:
        yield
    finally:
        if worker:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass


app = FastAPI(
    docs_url=None,
    redoc_url=None,
    title="Умный кредитный кабинет — демонстрационный API",
    version="1.0.0",
    description="Локальный учебный кредитный процесс. Синтетические данные, демонстрационные условия и операции.",
    lifespan=lifespan,
)


@app.exception_handler(HTTPException)
async def api_error(request: Request, exception: HTTPException):
    headers = exception.headers or {}
    message = exception.detail
    return JSONResponse(
        {
            "detail": message,
            "error": {"code": headers.get("X-Error-Code", f"HTTP_{exception.status_code}"), "message": message},
            "correlation_id": getattr(request.state, "correlation_id", None),
        },
        status_code=exception.status_code,
        headers=headers,
    )


@app.middleware("http")
async def headers_and_logs(request: Request, call_next):
    request_id = str(uuid4())
    request.state.correlation_id = request_id
    origin = request.headers.get("origin")
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and origin:
        parsed = urlsplit(origin)
        if parsed.netloc != request.url.netloc or parsed.scheme not in {"http", "https"}:
            return JSONResponse({"detail": "Запрос с другого сайта запрещён"}, status_code=403, headers={"X-Request-ID": request_id})
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("request_id=%s method=%s path=%s failed", request_id, request.method, request.url.path)
        response = JSONResponse(
            {"detail": "Внутренняя ошибка. Повторите запрос с тем же ключом операции.", "request_id": request_id}, status_code=500
        )
    response.headers.update(
        {"X-Request-ID": request_id, "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin", "X-Frame-Options": "DENY"}
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    logger.info("request_id=%s method=%s path=%s status=%s", request_id, request.method, request.url.path, response.status_code)
    return response


from .body_limit import BoundedBodyMiddleware

app.add_middleware(BoundedBodyMiddleware)


@app.get("/api/health/live")
def live():
    return {"status": "ok"}


@app.get("/api/health/ready")
def ready():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            connection.execute(text("SELECT version_num FROM alembic_version"))
        return {"status": "ready"}
    except Exception:
        raise HTTPException(503, "База данных недоступна или миграции не применены")


def rate_limit_login(db, email, ip, count_failure=True):
    instant = now()
    cutoff = instant - timedelta(minutes=5)
    limits = [(f"account:{email}", int(os.getenv("LOGIN_ACCOUNT_LIMIT", "5"))), (f"ip:{ip}", int(os.getenv("LOGIN_IP_LIMIT", "60")))]
    allowed = True
    for key, limit in limits:
        hashed = hashlib.sha256(key.encode()).hexdigest()
        if not count_failure:
            existing = db.get(LoginAttempt, hashed)
            if existing and utc(existing.window_start) >= cutoff and existing.count >= limit:
                raise HTTPException(429, "Слишком много попыток входа. Повторите через 5 минут.", headers={"Retry-After": "300"})
            continue
        insert = sqlite_insert(LoginAttempt) if SQLITE else postgres_insert(LoginAttempt)
        statement = (
            insert.values(id=hashed, count=1, window_start=instant)
            .on_conflict_do_update(
                index_elements=[LoginAttempt.id],
                set_={
                    "count": case((LoginAttempt.window_start < cutoff, 1), else_=LoginAttempt.count + 1),
                    "window_start": case((LoginAttempt.window_start < cutoff, instant), else_=LoginAttempt.window_start),
                },
            )
            .returning(LoginAttempt.count)
        )
        allowed = db.execute(statement).scalar_one() <= limit and allowed
    if count_failure:
        db.commit()  # Failed credentials must not roll back the durable limiter.
    if not allowed:
        raise HTTPException(429, "Слишком много попыток входа. Повторите через 5 минут.", headers={"Retry-After": "300"})


@app.post("/api/auth/login")
def login(body: Login, request: Request, response: Response, db=Depends(get_db, scope="function")):
    email = body.email.lower()
    rate_limit_login(db, email, request.client.host if request.client else "local", count_failure=False)
    user = db.scalar(select(User).where(User.email == email))
    verified = verify_password(body.password, user.password_hash if user else DUMMY_HASH)
    if user is None or not verified:
        rate_limit_login(db, email, request.client.host if request.client else "local", count_failure=True)
        raise HTTPException(401, "Неверная почта или пароль")
    account_key = hashlib.sha256(f"account:{email}".encode()).hexdigest()
    attempt = db.get(LoginAttempt, account_key)
    if attempt:
        attempt.count = 0
    token = secrets.token_urlsafe(32)
    session = Session(
        id=hashlib.sha256(token.encode()).hexdigest(),
        user_id=user.id,
        csrf_token=secrets.token_urlsafe(32),
        expires_at=now() + timedelta(hours=12),
    )
    db.add(session)
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
        max_age=43200,
        path="/",
    )
    return {"user": user_dict(user), "csrf_token": session.csrf_token}


@app.get("/api/auth/me")
def me(request: Request, user=Depends(get_user)):
    return {"user": user_dict(user), "csrf_token": request.state.session.csrf_token}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, user=Depends(get_user), db=Depends(get_db, scope="function")):
    db.delete(request.state.session)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/company")
def company(user=Depends(require_entrepreneur), db=Depends(get_db, scope="function")):
    return snapshot(db.get(Company, user.company_id))


@app.put("/api/company")
def update_company(body: CompanyEdit, user=Depends(require_entrepreneur), db=Depends(get_db, scope="function")):
    company = db.scalar(select(Company).where(Company.id == user.company_id).with_for_update())
    for key, value in body.model_dump().items():
        setattr(company, key, value)
    db.add(Audit(actor=user.name, action="company_updated", reason="Изменён профиль компании"))
    return snapshot(company)


@app.get("/api/products")
def products():
    return list(PRODUCTS.values())


@app.post("/api/calculate")
def calculation(body: Calculation, user=Depends(get_user)):
    return calculate(body.product_id, body.amount, body.months)


def get_application(db, app_id, user, lock=False):
    query = select(Application).where(Application.id == app_id)
    if user.role != "bank":
        query = query.where(Application.company_id == user.company_id)
    if lock:
        query = query.with_for_update()
    application = db.scalar(query)
    if not application:
        raise HTTPException(404, "Заявка не найдена")
    return application


def document_dict(document):
    return {key: getattr(document, key) for key in ("id", "name", "size")} | {"created_at": utc(document.created_at).isoformat()}


def serialize(db, application, user):
    db.flush()
    result = {
        key: getattr(application, key)
        for key in (
            "id",
            "number",
            "status",
            "version",
            "product_id",
            "months",
            "purpose",
            "company_snapshot",
            "calculation",
            "offer",
            "decision",
            "info_request",
            "history",
            "responses",
            "disbursement",
        )
    }
    result.update(
        {
            key: money(getattr(application, key)) if getattr(application, key) is not None else None
            for key in ("amount", "revenue", "expenses", "monthly_debt")
        }
    )
    result.update({key: utc(getattr(application, key)).isoformat() for key in ("created_at", "updated_at")})
    result["documents"] = [
        document_dict(d)
        for d in db.scalars(select(Document).where(Document.application_id == application.id).order_by(Document.created_at)).all()
    ]
    if user.role == "bank":
        result.update({key: getattr(application, key) for key in ("checks", "risk", "recommendation", "evaluation_history")})
        result["notes"] = [
            {"id": n.id, "actor": n.actor, "message": n.message, "created_at": utc(n.created_at).isoformat()}
            for n in db.scalars(select(Note).where(Note.application_id == application.id).order_by(Note.created_at)).all()
        ]
    return result


def idempotency_request(db, request, user, scope, body):
    key = request.headers.get("Idempotency-Key", "")
    if not key or len(key) > 128:
        raise HTTPException(400, "Передайте Idempotency-Key длиной от 1 до 128 символов")
    digest = hashlib.sha256(json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    existing = db.scalar(select(Idempotency).where(Idempotency.user_id == user.id, Idempotency.scope == scope, Idempotency.key == key))
    if existing:
        if existing.request_hash != digest:
            raise HTTPException(409, "Этот ключ операции уже использован с другими данными")
        return key, digest, existing.response
    return key, digest, None


def save_idempotency(db, user, scope, key, digest, result):
    db.add(Idempotency(user_id=user.id, scope=scope, key=key, request_hash=digest, response=result))


def check_version(application, version):
    if application.version != version:
        raise HTTPException(409, "Заявка уже изменена. Обновите карточку и повторите действие.")


@app.get("/api/applications")
def applications(
    status: str | None = None,
    product_id: str | None = None,
    attention: bool = False,
    user=Depends(get_user),
    db=Depends(get_db, scope="function"),
):
    query = select(Application)
    if user.role != "bank":
        query = query.where(Application.company_id == user.company_id)
    if status:
        query = query.where(Application.status == status)
    if product_id:
        query = query.where(Application.product_id == product_id)
    if attention:
        query = query.where(Application.status.in_(ATTENTION_STATUSES))
    return [serialize(db, a, user) for a in db.scalars(query.order_by(Application.updated_at.desc())).all()]


@app.post("/api/applications", status_code=201)
def create_application(body: ApplicationCreate, request: Request, user=Depends(require_entrepreneur), db=Depends(get_db, scope="function")):
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    key, digest, replay = idempotency_request(db, request, user, "create", body)
    if replay is not None:
        return replay
    result = calculate(body.product_id, body.amount, body.months)
    company = db.get(Company, user.company_id)
    application = Application(
        number="КР-" + uuid4().hex[:10].upper(),
        company_id=company.id,
        company_snapshot=snapshot(company),
        calculation=result,
        **body.model_dump(),
    )
    db.add(application)
    db.flush()
    transition(db, application, "draft", user.name, "Создан черновик заявки")
    data = serialize(db, application, user)
    save_idempotency(db, user, "create", key, digest, data)
    return data


@app.get("/api/applications/{app_id}")
def application_detail(app_id: str, user=Depends(get_user), db=Depends(get_db, scope="function")):
    return serialize(db, get_application(db, app_id, user), user)


@app.put("/api/applications/{app_id}")
def edit_application(app_id: str, body: ApplicationEdit, user=Depends(require_entrepreneur), db=Depends(get_db, scope="function")):
    application = get_application(db, app_id, user, True)
    check_version(application, body.version)
    if application.status != "draft":
        raise HTTPException(409, "Изменять поля можно только в черновике")
    application.calculation = calculate(body.product_id, body.amount, body.months)
    for key, value in body.model_dump(exclude={"version"}).items():
        setattr(application, key, value)
    application.company_snapshot = snapshot(db.get(Company, user.company_id))
    application.version += 1
    application.updated_at = now()
    db.add(Audit(application_id=app_id, actor=user.name, action="draft_updated", reason="Черновик сохранён"))
    return serialize(db, application, user)


@app.post("/api/applications/{app_id}/actions")
def action(app_id: str, body: Action, request: Request, user=Depends(get_user), db=Depends(get_db, scope="function")):
    application = get_application(db, app_id, user, True)
    scope = f"action:{app_id}"
    key, digest, replay = idempotency_request(db, request, user, scope, body)
    if replay is not None:
        return replay
    check_version(application, body.version)
    command = body.action
    if command in BANK_ACTIONS:
        if user.role != "bank":
            raise HTTPException(403, "Решение может принять только сотрудник банка")
        if application.status != "manual_review":
            raise HTTPException(409, "Ручное решение доступно только на этапе рассмотрения сотрудником")
        if not body.reason or len(body.reason) < 3:
            raise HTTPException(422, "Укажите обоснование решения (не менее 3 символов)")
        if command == "request_info":
            application.info_request = body.message or body.reason
            transition(db, application, "needs_info", user.name, application.info_request)
        else:
            application.offer = None
            if command == "offer":
                if body.amount is None or body.months is None:
                    raise HTTPException(422, "Укажите сумму и срок предложения")
                application.offer = {"amount": money(body.amount), "months": body.months} | calculate(
                    application.product_id, body.amount, body.months
                )
            application.decision = {"type": command, "reason": body.reason, "source": "manual"}
            transition(
                db,
                application,
                {"approve": "approved", "reject": "rejected", "offer": "alternative_offered"}[command],
                user.name,
                body.reason,
            )
    else:
        if user.role != "entrepreneur":
            raise HTTPException(403, "Это действие доступно только владельцу компании")
        if application.status not in ENTREPRENEUR_TRANSITIONS[command]:
            raise HTTPException(409, "Действие недоступно в текущем статусе заявки")
        if command == "submit":
            company = db.get(Company, application.company_id)
            application.company_snapshot = snapshot(company)
            application.calculation = calculate(application.product_id, application.amount, application.months)
            transition(db, application, "submitted", user.name, "Заявка отправлена на рассмотрение")
            process(db, application, company)
        elif command == "retry":
            db.add(Audit(application_id=app_id, actor=user.name, action="retry", reason="Повторная обработка после сбоя проверки"))
            process(db, application, db.get(Company, application.company_id))
        elif command == "respond":
            if not body.message or len(body.message) < 3:
                raise HTTPException(422, "Напишите ответ на запрос (не менее 3 символов)")
            for name in ("revenue", "expenses", "monthly_debt"):
                value = getattr(body, name)
                if value is not None:
                    setattr(application, name, value)
            application.responses = list(application.responses) + [{"message": body.message, "created_at": now().isoformat()}]
            db.add(Audit(application_id=app_id, actor=user.name, action="respond", reason=body.message))
            process(db, application, db.get(Company, application.company_id))
        elif command == "disburse":
            terms = application.offer
            application.disbursement = {
                "reference": "DEMO-" + uuid4().hex[:12].upper(),
                "amount": terms["amount"] if terms else money(application.amount),
                "created_at": now().isoformat(),
            }
            transition(db, application, "disbursed", user.name, "Смоделированная выдача завершена. Реальные деньги не перечислялись.")
        else:
            reasons = {
                "accept": "Предприниматель принял условия финансирования",
                "decline": "Предприниматель отклонил предложение",
                "sign": "Демонстрационное подписание подтверждено пользователем; юридической силы не имеет",
                "cancel": "Заявка отменена предпринимателем",
            }
            reason = reasons[command] + (f": {body.reason}" if command in {"decline", "cancel"} and body.reason else "")
            transition(
                db,
                application,
                {"accept": "accepted", "decline": "declined", "sign": "signed", "cancel": "cancelled"}[command],
                user.name,
                reason,
            )
    application.version += 1
    data = serialize(db, application, user)
    save_idempotency(db, user, scope, key, digest, data)
    return data


@app.get("/api/applications/{app_id}/documents")
def documents(app_id: str, user=Depends(get_user), db=Depends(get_db, scope="function")):
    get_application(db, app_id, user)
    return [document_dict(d) for d in db.scalars(select(Document).where(Document.application_id == app_id)).all()]


def validate_file(filename, content):
    suffix = Path(filename or "").suffix.lower()
    types = {
        ".txt": "text/plain; charset=utf-8",
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }
    if suffix not in types:
        raise HTTPException(422, "Разрешены только PDF, TXT, PNG и JPG")
    if not content:
        raise HTTPException(422, "Файл пуст")
    if suffix == ".pdf" and not (content.startswith(b"%PDF-") and b"%%EOF" in content[-2048:]):
        raise HTTPException(422, "Содержимое не соответствует PDF")
    if suffix == ".png" and not (content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) > 24 and content[12:16] == b"IHDR"):
        raise HTTPException(422, "Содержимое не соответствует PNG")
    if suffix in {".jpg", ".jpeg"} and not (content.startswith(b"\xff\xd8\xff") and content.endswith(b"\xff\xd9")):
        raise HTTPException(422, "Содержимое не соответствует JPG")
    if suffix == ".txt":
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(422, "TXT должен быть в кодировке UTF-8")
        if any(ord(ch) < 32 and ch not in "\n\r\t" for ch in decoded):
            raise HTTPException(422, "TXT содержит недопустимые бинарные символы")
    return suffix, types[suffix]


@app.post("/api/applications/{app_id}/documents", status_code=201)
async def upload_document(
    app_id: str, file: UploadFile = File(...), user=Depends(require_entrepreneur), db=Depends(get_db, scope="function")
):
    application = get_application(db, app_id, user, True)
    if application.status not in {"draft", "needs_info"}:
        raise HTTPException(409, "Документы можно добавить в черновик или в ответ на запрос сведений")
    if len(db.scalars(select(Document.id).where(Document.application_id == app_id)).all()) >= 20:
        raise HTTPException(422, "В заявке может быть не более 20 документов")
    content = await file.read(5 * 1024 * 1024 + 1)
    await file.close()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(413, "Максимальный размер файла — 5 МиБ")
    suffix, media_type = validate_file(file.filename, content)
    document = Document(
        application_id=app_id, name="document-" + uuid4().hex[:12] + suffix, media_type=media_type, size=len(content), content=content
    )
    db.add(document)
    application.version += 1
    application.updated_at = now()
    db.add(Audit(application_id=app_id, actor=user.name, action="document_uploaded", reason="Добавлен документ: " + suffix))
    db.flush()
    return document_dict(document)


@app.get("/api/documents/{document_id}")
def download_document(document_id: str, user=Depends(get_user), db=Depends(get_db, scope="function")):
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "Документ не найден")
    get_application(db, document.application_id, user)
    return Response(
        content=document.content,
        media_type=document.media_type,
        headers={"Content-Disposition": f'attachment; filename="{document.name}"', "X-Content-Type-Options": "nosniff"},
    )


@app.post("/api/applications/{app_id}/notes", status_code=201)
def add_note(app_id: str, body: NoteCreate, user=Depends(require_bank), db=Depends(get_db, scope="function")):
    get_application(db, app_id, user, True)
    note = Note(application_id=app_id, actor=user.name, message=body.message)
    db.add(note)
    db.add(Audit(application_id=app_id, actor=user.name, action="internal_note", reason="Добавлен внутренний комментарий"))
    db.flush()
    return {"id": note.id, "message": note.message, "actor": note.actor, "created_at": utc(note.created_at).isoformat()}


@app.get("/api/notifications")
def notifications(user=Depends(get_user), db=Depends(get_db, scope="function")):
    return [
        {"id": n.id, "application_id": n.application_id, "message": n.message, "created_at": utc(n.created_at).isoformat(), "read": n.read}
        for n in db.scalars(
            select(Notification).where(Notification.user_id == user.id).order_by(Notification.created_at.desc()).limit(100)
        ).all()
    ]


@app.post("/api/notifications/{notification_id}/read")
def read_notification(notification_id: str, user=Depends(get_user), db=Depends(get_db, scope="function")):
    notification = db.scalar(
        select(Notification).where(Notification.id == notification_id, Notification.user_id == user.id).with_for_update()
    )
    if not notification:
        raise HTTPException(404, "Уведомление не найдено")
    notification.read = True
    return {"ok": True}


@app.get("/api/bank/summary")
def bank_summary(user=Depends(require_bank), db=Depends(get_db, scope="function")):
    applications = db.scalars(select(Application)).all()
    return {
        "total": len(applications),
        "attention": sum(a.status in ATTENTION_STATUSES for a in applications),
        "approved": sum(a.status in {"approved", "accepted", "signed", "disbursed"} for a in applications),
        "disbursed": sum(a.status == "disbursed" for a in applications),
        "requested_amount": money(sum((a.amount for a in applications), 0)),
    }


@app.get("/api/bank/audit")
def bank_audit(user=Depends(require_bank), db=Depends(get_db, scope="function")):
    return [
        {
            "id": a.id,
            "application_id": a.application_id,
            "actor": a.actor,
            "action": a.action,
            "reason": a.reason,
            "created_at": utc(a.created_at).isoformat(),
        }
        for a in db.scalars(select(Audit).order_by(Audit.created_at.desc()).limit(300)).all()
    ]


SWAGGER_PATH = BACKEND_ROOT / "app" / "static" / "swagger-ui"
app.mount("/swagger-static", StaticFiles(directory=SWAGGER_PATH, check_dir=False), name="swagger-static")


@app.get("/swagger-init.js", include_in_schema=False)
def swagger_init():
    return FileResponse(BACKEND_ROOT / "app" / "static" / "swagger-init.js", media_type="application/javascript")


@app.get("/docs", include_in_schema=False)
def swagger_docs():
    if not SWAGGER_PATH.is_dir():
        return JSONResponse(
            {"detail": "Локальные ресурсы Swagger UI отсутствуют", "openapi": "/openapi.json"},
            status_code=503,
        )
    return HTMLResponse(
        """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Умный кредитный кабинет — OpenAPI</title><link rel="stylesheet" href="/swagger-static/swagger-ui.css"></head><body><div id="swagger-ui"></div><script src="/swagger-static/swagger-ui-bundle.js"></script><script src="/swagger-init.js"></script></body></html>"""
    )


from .v2 import router as credit_cycle_router

app.include_router(credit_cycle_router)
# The supplied TZ names v1; v2 keeps the first prototype API fully compatible.
# Both prefixes below expose the same credit-cycle commands and authorization.
for credit_route in credit_cycle_router.routes:
    app.add_api_route(
        credit_route.path.replace("/api/v2", "/api/v1", 1),
        credit_route.endpoint,
        methods=list(credit_route.methods),
        status_code=credit_route.status_code,
        response_model=credit_route.response_model,
        tags=["Кредитный цикл · API v1"],
    )
app.add_api_route("/api/v1/auth/login", login, methods=["POST"])
app.add_api_route("/api/v1/auth/logout", logout, methods=["POST"])
app.add_api_route("/api/v1/me", me, methods=["GET"])

DIST = BACKEND_ROOT.parent / "frontend" / "dist"
if (DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(DIST / "assets")), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def frontend(path: str):
    if path == "api" or path.startswith("api/"):
        raise HTTPException(404, "API endpoint не найден")
    target = (DIST / path).resolve()
    if target.is_relative_to(DIST.resolve()) and target.is_file():
        return FileResponse(target)
    if (DIST / "index.html").exists():
        return FileResponse(DIST / "index.html")
    return JSONResponse({"message": "API запущен. Соберите frontend для веб-интерфейса.", "docs": "/docs"})
