#!/usr/bin/env python3
"""Non-destructive restart proof on an explicitly selected local QA Compose stack."""

import argparse
import hashlib
import importlib.util
import json
import subprocess
import time
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlparse
import httpx

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("demo_commands", ROOT / "scripts" / "prepare-v2-demo.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def wait(client, path, predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = helper.call(client, "GET", path)
        if predicate(value):
            return value
        time.sleep(0.2)
    raise RuntimeError("Expected state not reached: " + path)


def submit(client, letter):
    a = helper.call(
        client, "POST", "/api/v2/applications", {"template_id": "FIX-" + letter, "organization_id": "demo-org-" + letter.lower()}
    )
    value = deepcopy(a["input"])
    value["financials_confirmed"] = True
    value["cycle_plan"]["confirmed"] = True
    a = helper.call(client, "PUT", f"/api/v2/applications/{a['id']}", {"expected_version": a["revision"], "input": value})
    for purpose in ("DATA_PROCESSING", "CREDIT_HISTORY"):
        a = helper.command(client, a, "consent", purpose=purpose, confirmed=True)
    return helper.command(client, a, "submit")


def run(url, project):
    with httpx.Client(base_url=url, timeout=20) as line, httpx.Client(base_url=url, timeout=20) as source:
        helper.login(line, "fix-k")
        a = submit(line, "K")
        a = wait(line, f"/api/v2/applications/{a['id']}", lambda x: x["status"] == "OFFER_READY")
        a = helper.command(line, a, "accept", offer_id=a["offer"]["id"], offer_hash=a["offer"]["hash"], confirmed=True)
        a = helper.command(line, a, "sign", bundle_hash=a["bundle"]["hash"], confirmed=True)
        a = wait(line, f"/api/v2/applications/{a['id']}", lambda x: x["status"] == "CONTRACT_ACTIVE")
        path = f"/api/v2/contracts/{a['contract_id']}"
        c = helper.call(line, "GET", path)
        plan = deepcopy(a["input"]["cycle_plan"])
        for event in plan["events"]:
            if event["kind"] == "TARGET_PAYMENT":
                event["amount_kopecks"] = 60000000
            elif event["id"] == "buyer-30":
                event["amount_kopecks"] = 58000000
        q = helper.call(
            line,
            "POST",
            path + "/commands",
            {"action": "draw_preview", "expected_version": c["revision"], "amount_kopecks": 60000000, "days": 30, "cycle_plan": plan},
        )["quote"]
        helper.call(
            line, "POST", path + "/commands", {"action": "draw", "expected_version": c["revision"], "quote_id": q["id"], "confirmed": True}
        )
        before = wait(line, path, lambda x: x["facility"]["drawn_kopecks"] == 60000000)
        document = a["documents"][0]["id"]
        original = line.get("/api/v2/documents/" + document + "/content")
        original.raise_for_status()
        helper.login(source, "fix-i")
        pending = submit(source, "I")
        pending = wait(source, f"/api/v2/applications/{pending['id']}", lambda x: x["status"] == "WAITING_SOURCE")
        subprocess.run(
            ["docker", "compose", "-p", project, "-f", "docker-compose.yml", "-f", "docker-compose.test.yml", "restart", "app", "db"],
            cwd=ROOT,
            check=True,
            timeout=60,
        )
        deadline = time.monotonic() + 75
        while True:
            try:
                if line.get("/api/health/ready").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("Services not ready after restart")
            time.sleep(0.5)
        after = helper.call(line, "GET", path)
        assert before == after, "Contract, money or history changed across restart"
        content = line.get("/api/v2/documents/" + document + "/content")
        content.raise_for_status()
        assert content.content == original.content, "Document bytes changed"
        resumed = wait(source, f"/api/v2/applications/{pending['id']}", lambda x: x["status"] == "OFFER_READY")
        assert len(resumed["versions"]) == len(resumed["offers"]) == 1
        attempts = [x for x in resumed["checks"] if x["source"] == "credit_history"]
        assert [x["status"] for x in attempts] == ["FAILED_RETRYABLE", "FAILED_RETRYABLE", "SUCCEEDED"]
        assert len([o for o in after["operations"] if o["kind"] == "TRANCHE_DISBURSEMENT"]) == 1
        return {
            "result": "PASS",
            "contract_id": after["id"],
            "principal_kopecks": after["facility"]["drawn_kopecks"],
            "available_kopecks": after["facility"]["available_kopecks"],
            "reserved_kopecks": after["facility"]["reserved_kopecks"],
            "document_sha256": hashlib.sha256(content.content).hexdigest(),
            "source_attempts": [x["status"] for x in attempts],
            "session_survived": True,
            "single_disbursement": True,
            "application_versions": len(resumed["versions"]),
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    if urlparse(args.url).hostname not in {"localhost", "127.0.0.1"} or not args.project.startswith("smart-credit-v2-"):
        raise SystemExit("Use an explicitly named local smart-credit-v2-* QA stack")
    result = run(args.url, args.project)
    print(json.dumps(result, ensure_ascii=False, indent=2))
