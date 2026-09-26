from __future__ import annotations

import argparse
import json

from fastapi.testclient import TestClient

from app.database import database_path, get_connection, init_db
from app.main import app
from app.temple.schema import ensure_temple_schema


def command_init() -> int:
    init_db()
    ensure_temple_schema(get_connection())
    print(json.dumps({"database": str(database_path()), "status": "initialized"}, ensure_ascii=False))
    return 0


def command_check() -> int:
    init_db()
    connection = get_connection()
    ensure_temple_schema(connection)
    result = {
        "database": str(database_path()),
        "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        "tables": connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["integrity"] == "ok" and result["foreign_keys"] == 1 else 1


def command_smoke() -> int:
    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
        summary = client.get("/api/temple/summary")
    result = {
        "root": root.json(),
        "health": health.json(),
        "summary": summary.json(),
        "status_codes": [root.status_code, health.status_code, summary.status_code],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status_codes"] == [200, 200, 200] else 1


def command_temple_demo() -> int:
    with TestClient(app) as client:
        seeded = client.post("/api/temple/demo/seed")
        if seeded.status_code != 200:
            print(seeded.text)
            return 1
        now = "2026-09-26T05:30:00Z"
        authorization = client.post(
            "/api/temple/authorizations",
            json={
                "steward_hash": "steward-demo-0000000001",
                "temple_code": "lingyun-temple",
                "authorization_code": "festival-duty",
                "valid_from": "2026-09-26T00:00:00Z",
                "valid_until": "2026-09-27T00:00:00Z",
                "source_approval_id": "demo-approval-000001",
            },
        )
        observation = client.post(
            "/api/temple/observations",
            json={
                "observation_key": "demo-observation-000001",
                "temple_code": "lingyun-temple",
                "hall_code": "main-hall",
                "incense_code": "festival-incense",
                "steward_hash": "steward-demo-0000000001",
                "sensor_class": "ceiling-sensor",
                "visitor_density": 300,
                "pm25_ugm3": 380,
                "co_ppm": 0.08,
                "supply_airflow": 1.5,
                "exhaust_airflow": 0.5,
                "observed_at": now,
            },
        )
        incident_id = observation.json().get("safety_incident_id")
        started = client.post(f"/api/temple/safety_incidents/{incident_id}/mitigate", json={"actor": "cli-demo"})
    result = {
        "seed": seeded.status_code,
        "authorization": authorization.status_code,
        "observation": observation.status_code,
        "safety_incident_id": incident_id,
        "mitigation": started.status_code,
        "mitigation_session_id": started.json().get("id") if started.status_code == 200 else None,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if [seeded.status_code, authorization.status_code, observation.status_code, started.status_code] == [200, 201, 202, 200] else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="temple-stewardship", description="寺院香火与修缮协同服务维护入口")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化 SQLite 数据库")
    subparsers.add_parser("check-db", help="检查数据库完整性")
    subparsers.add_parser("smoke", help="执行本地 API 冒烟检查")
    subparsers.add_parser("temple-demo", help="执行香火隐患识别与通风处置演示")
    args = parser.parse_args()
    return {
        "init-db": command_init,
        "check-db": command_check,
        "smoke": command_smoke,
        "temple-demo": command_temple_demo,
    }[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
