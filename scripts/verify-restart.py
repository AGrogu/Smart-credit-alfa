"""Non-destructive verification of this Compose project's persisted process.

Run after `docker compose up -d`: .venv/bin/python scripts/verify-restart.py
Only the project's app and db services are restarted. No rows or volumes removed.
"""

import json
import os
import subprocess
import time
from uuid import uuid4
import httpx

base = os.getenv("VERIFY_BASE_URL", "http://127.0.0.1:8080")
with httpx.Client(base_url=base, timeout=20) as client:
    auth = client.post("/api/auth/login", json={"email": "good@demo.local", "password": "Demo2026!"})
    auth.raise_for_status()
    client.headers["X-CSRF-Token"] = auth.json()["csrf_token"]
    payload = {
        "product_id": "working",
        "amount": "500000",
        "months": 12,
        "purpose": "Проверка сохранности при перезапуске",
        "revenue": "1000000",
        "expenses": "600000",
        "monthly_debt": "20000",
    }
    created = client.post("/api/applications", json=payload, headers={"Idempotency-Key": str(uuid4())})
    created.raise_for_status()
    app_id = created.json()["id"]
    upload = client.post(
        f"/api/applications/{app_id}/documents", files={"file": ("synthetic.txt", b"Synthetic restart verification document", "text/plain")}
    )
    upload.raise_for_status()
    data = client.get(f"/api/applications/{app_id}").json()
    for action in ["submit", "accept", "sign", "disburse"]:
        result = client.post(
            f"/api/applications/{app_id}/actions",
            json={"action": action, "version": data["version"]},
            headers={"Idempotency-Key": str(uuid4())},
        )
        result.raise_for_status()
        data = result.json()
    document = client.get(f"/api/documents/{data['documents'][0]['id']}").content
    subprocess.run(["docker", "compose", "restart", "db", "app"], check=True)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if client.get("/api/health/ready").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(1)
    else:
        raise RuntimeError("Readiness did not recover within 60 seconds")
    after = client.get(f"/api/applications/{app_id}")
    after.raise_for_status()
    assert after.json() == data, "State changed or disappeared after restart"
    assert client.get(f"/api/documents/{data['documents'][0]['id']}").content == document
    print(
        json.dumps(
            {
                "result": "passed",
                "application": app_id,
                "status": data["status"],
                "document_bytes": len(document),
                "checked": ["session", "application", "history", "single_disbursement", "document_content"],
            },
            ensure_ascii=False,
        )
    )
