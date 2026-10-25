#!/usr/bin/env python3
"""Prepare only missing synthetic examples through the real local API; never reset."""

import argparse
import json
import time
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4
import httpx

ROOT = Path(__file__).resolve().parents[1]
TERMINAL_PROCESS = {
    "OFFER_READY",
    "MANUAL_REVIEW",
    "NEEDS_INFO",
    "NOT_ELIGIBLE",
    "DECLINED",
    "CONTRACT_ACTIVE",
    "OFFER_EXPIRED",
    "OFFER_REJECTED",
    "WITHDRAWN",
}


def call(client, method, path, body=None):
    headers = {"Idempotency-Key": str(uuid4())} if method != "GET" else {}
    for attempt in range(3):
        try:
            response = client.request(method, path, json=body, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException:
            if attempt == 2:
                raise


def login(client, account):
    response = call(client, "POST", "/api/auth/login", {"email": account + "@demo.local", "password": "Demo2026!"})
    client.headers["X-CSRF-Token"] = response["csrf_token"]


def command(client, a, action, **extra):
    return call(client, "POST", f"/api/v2/applications/{a['id']}/commands", {"action": action, "expected_version": a["revision"], **extra})


def prepare(base_url):
    result = []
    for account, letter in [
        ("good", "A"),
        ("offer", "B"),
        ("review", "C"),
        ("good", "D"),
        ("info", "E"),
        ("fix-f", "F"),
        ("risk", "G"),
        ("fix-h", "H"),
        ("retry", "I"),
        ("fix-j", "J"),
        ("fix-k", "K"),
        ("fix-l", "L"),
    ]:
        with httpx.Client(base_url=base_url, timeout=20) as client:
            login(client, account)
            workspace = call(client, "GET", f"/api/v2/workspace?organization_id=demo-org-{letter.lower()}")
            if workspace["applications"]:
                a = workspace["applications"][0]
                result.append({"profile": "FIX-" + letter, "application_id": a["id"], "status": a["status"], "created": False})
                continue
            a = call(
                client, "POST", "/api/v2/applications", {"template_id": "FIX-" + letter, "organization_id": "demo-org-" + letter.lower()}
            )
            value = deepcopy(a["input"])
            value["financials_confirmed"] = True
            value["cycle_plan"]["confirmed"] = True
            a = call(client, "PUT", f"/api/v2/applications/{a['id']}", {"expected_version": a["revision"], "input": value})
            for purpose in ("DATA_PROCESSING", "CREDIT_HISTORY"):
                a = command(client, a, "consent", purpose=purpose, confirmed=True)
            a = command(client, a, "submit")
            deadline = time.monotonic() + 25
            while a["status"] not in TERMINAL_PROCESS and time.monotonic() < deadline:
                time.sleep(0.25)
                a = call(client, "GET", f"/api/v2/applications/{a['id']}")
            result.append({"profile": "FIX-" + letter, "application_id": a["id"], "status": a["status"], "created": True})
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    if urlparse(args.url).hostname not in {"localhost", "127.0.0.1"}:
        raise SystemExit("This synthetic preparation script supports localhost only")
    results = prepare(args.url)
    output = ROOT / "work" / "v2-demo-state.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    for r in results:
        print(r["profile"], r["status"], args.url + "/#credit-cycle/application/" + r["application_id"])
