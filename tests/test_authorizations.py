from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest

from app.core.clock import FrozenClock
from app.database import get_connection
from app.core.errors import ConflictError
from app.temple.service import TempleSafetyService


def ensure_temple(client, code: str = "lingyun-temple") -> None:
    response = client.post(
        "/api/temple/temples",
        json={
            "code": code,
            "name": "凌云古寺",
            "temple_type": "heritage",
            "timezone": "Asia/Shanghai",
            "max_concurrent_mitigation_sessions": 10,
            "ventilation_capacity": 3000,
        },
    )
    assert response.status_code in (201, 409)


def authorization_payload(**overrides):
    payload = {
        "steward_hash": "steward-aaaa-00000001",
        "temple_code": "lingyun-temple",
        "authorization_code": "festival-duty",
        "valid_from": "2026-09-26T00:00:00Z",
        "valid_until": "2026-10-27T00:00:00Z",
        "source_approval_id": "approval-immutable-0001",
    }
    payload.update(overrides)
    return payload


def test_created_vs_idempotent_retry_are_distinguishable(client):
    ensure_temple(client)
    first = client.post("/api/temple/authorizations", json=authorization_payload())
    assert first.status_code == 201, first.text
    assert first.json()["request_outcome"] == "created"
    first_id = first.json()["id"]

    replay = client.post("/api/temple/authorizations", json=authorization_payload())
    assert replay.status_code == 200, replay.text
    body = replay.json()
    assert body["request_outcome"] == "idempotent_retry"
    assert body["id"] == first_id
    assert body["state"] == "active"

    # 同一时刻的不同时区写法仍属于同一次申请。
    shifted = client.post(
        "/api/temple/authorizations",
        json=authorization_payload(valid_from="2026-09-26T08:00:00+08:00", valid_until="2026-10-27T08:00:00+08:00"),
    )
    assert shifted.status_code == 200
    assert shifted.json()["id"] == first_id


def test_content_conflict_is_rejected_with_masked_summary(client):
    ensure_temple(client)
    first = client.post("/api/temple/authorizations", json=authorization_payload())
    assert first.status_code == 201

    tampered = client.post(
        "/api/temple/authorizations",
        json=authorization_payload(steward_hash="steward-bbbb-00000002", valid_until="2026-11-30T00:00:00Z"),
    )
    assert tampered.status_code == 409, tampered.text
    error = tampered.json()["error"]
    assert error["context"]["source_approval_id"] == "approval-immutable-0001"
    fields = {item["field"]: item for item in error["context"]["differences"]}
    assert set(fields) == {"steward_hash", "valid_until"}
    # 差异摘要足以定位，但不得暴露完整人员标识。
    assert "steward-bbbb-00000002" not in tampered.text
    assert "steward-aaaa-00000001" not in tampered.text
    assert fields["steward_hash"]["existing"].startswith("stew")
    assert "***" in fields["steward_hash"]["existing"]
    assert fields["valid_until"]["existing"] == "2026-10-27T00:00:00+00:00"
    assert fields["valid_until"]["incoming"] == "2026-11-30T00:00:00+00:00"

    # 原始记录必须保持不变。
    connection = get_connection()
    rows = connection.execute(
        "SELECT * FROM steward_authorizations WHERE source_approval_id=?",
        ("approval-immutable-0001",),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["steward_hash"] == "steward-aaaa-00000001"
    assert rows[0]["valid_until"] == "2026-10-27T00:00:00+00:00"

    conflict = connection.execute(
        "SELECT * FROM authorization_conflicts WHERE source_approval_id=?",
        ("approval-immutable-0001",),
    ).fetchone()
    assert conflict is not None
    assert conflict["steward_hint"] != "steward-bbbb-00000002"
    differences = json.loads(conflict["differences_json"])
    assert {item["field"] for item in differences} == {"steward_hash", "valid_until"}


def test_conflict_on_each_identity_field(client):
    ensure_temple(client)
    client.post("/api/temple/authorizations", json=authorization_payload(source_approval_id="approval-fields"))
    cases = {
        "steward_hash": {"steward_hash": "steward-cccc-00000003"},
        "temple_code": {"temple_code": "other-temple"},
        "authorization_code": {"authorization_code": "night-duty"},
        "valid_from": {"valid_from": "2026-09-25T00:00:00Z"},
        "valid_until": {"valid_until": "2026-10-28T00:00:00Z"},
    }
    for field, override in cases.items():
        if field == "temple_code":
            client.post(
                "/api/temple/temples",
                json={"code": "other-temple", "name": "其他寺院", "temple_type": "urban", "max_concurrent_mitigation_sessions": 1, "ventilation_capacity": 100},
            )
        response = client.post(
            "/api/temple/authorizations",
            json=authorization_payload(source_approval_id="approval-fields", **override),
        )
        assert response.status_code == 409, (field, response.text)
        changed = {item["field"] for item in response.json()["error"]["context"]["differences"]}
        assert changed == {field}


def test_replay_after_suspend_cancel_or_expire_never_changes_state(client):
    ensure_temple(client)
    created = client.post("/api/temple/authorizations", json=authorization_payload(source_approval_id="approval-lifecycle"))
    authorization_id = created.json()["id"]

    suspended = client.post(
        f"/api/temple/authorizations/{authorization_id}/suspend",
        json={"actor": "warden", "reason": "例行核查暂停"},
    )
    assert suspended.status_code == 200
    assert suspended.json()["state"] == "suspended"

    replay = client.post("/api/temple/authorizations", json=authorization_payload(source_approval_id="approval-lifecycle"))
    assert replay.status_code == 200
    assert replay.json()["state"] == "suspended"
    assert replay.json()["request_outcome"] == "idempotent_retry"

    # 即使携带篡改内容，旧授权状态也不改变，只产生冲突记录。
    tampered = client.post(
        "/api/temple/authorizations",
        json=authorization_payload(source_approval_id="approval-lifecycle", authorization_code="night-duty"),
    )
    assert tampered.status_code == 409
    assert get_connection().execute(
        "SELECT state FROM steward_authorizations WHERE id=?", (authorization_id,)
    ).fetchone()["state"] == "suspended"

    cancelled = client.post(
        f"/api/temple/authorizations/{authorization_id}/cancel",
        json={"actor": "warden", "reason": "审批撤销"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    replay_cancelled = client.post("/api/temple/authorizations", json=authorization_payload(source_approval_id="approval-lifecycle"))
    assert replay_cancelled.json()["state"] == "cancelled"

    # 到期：有效期落在过去的授权由回收任务标记，之后重放仍是到期态。
    expired_create = client.post(
        "/api/temple/authorizations",
        json=authorization_payload(
            source_approval_id="approval-expired",
            valid_from="2026-09-01T00:00:00Z",
            valid_until="2026-09-02T00:00:00Z",
        ),
    )
    assert expired_create.status_code == 201
    reaped = TempleSafetyService(
        get_connection(), FrozenClock(datetime(2026, 9, 28, tzinfo=UTC))
    ).authorizations.expire_due_authorizations("tests")
    assert expired_create.json()["id"] in reaped["expired"]
    replay_expired = client.post(
        "/api/temple/authorizations",
        json=authorization_payload(
            source_approval_id="approval-expired",
            valid_from="2026-09-01T00:00:00Z",
            valid_until="2026-09-02T00:00:00Z",
        ),
    )
    assert replay_expired.status_code == 200
    assert replay_expired.json()["state"] == "expired"


def test_audit_events_distinguish_created_replay_and_conflict(client):
    ensure_temple(client)
    client.post("/api/temple/authorizations", json=authorization_payload(source_approval_id="approval-audit"))
    client.post("/api/temple/authorizations", json=authorization_payload(source_approval_id="approval-audit"))
    client.post(
        "/api/temple/authorizations",
        json=authorization_payload(source_approval_id="approval-audit", valid_until="2026-12-01T00:00:00Z"),
    )
    connection = get_connection()
    rows = connection.execute(
        "SELECT action,outcome,metadata_json,after_json FROM audit_events "
        "WHERE resource_type='steward_authorization' AND resource_id=? ORDER BY id",
        (str(connection.execute("SELECT id FROM steward_authorizations WHERE source_approval_id='approval-audit'").fetchone()[0]),),
    ).fetchall()
    actions = [(row["action"], row["outcome"]) for row in rows]
    assert actions == [
        ("authorization.created", "success"),
        ("authorization.replayed", "success"),
        ("authorization.conflict", "failure"),
    ]
    outcomes = [json.loads(row["metadata_json"])["request_outcome"] for row in rows]
    assert outcomes == ["created", "idempotent_retry", "content_conflict"]
    # 审计留存中不得出现完整人员标识。
    for row in rows:
        assert "steward-aaaa-00000001" not in (row["metadata_json"] or "")
        assert "steward-aaaa-00000001" not in (row["after_json"] or "")


def test_concurrent_same_content_leaves_single_credible_version(client):
    ensure_temple(client)
    barrier = threading.Barrier(2)
    results: list[dict] = []

    def worker() -> None:
        service = TempleSafetyService(get_connection())
        barrier.wait()
        results.append(service.add_authorization(authorization_payload(source_approval_id="approval-race-same")))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 2
    assert {item["request_outcome"] for item in results} == {"created", "idempotent_retry"}
    assert results[0]["id"] == results[1]["id"]
    connection = get_connection()
    assert connection.execute(
        "SELECT COUNT(*) FROM steward_authorizations WHERE source_approval_id='approval-race-same'"
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM authorization_conflicts WHERE source_approval_id='approval-race-same'"
    ).fetchone()[0] == 0


def test_concurrent_different_content_only_one_wins(client):
    ensure_temple(client)
    barrier = threading.Barrier(2)
    errors: list[ConflictError] = []
    winners: list[dict] = []

    def worker(steward_hash: str) -> None:
        service = TempleSafetyService(get_connection())
        barrier.wait()
        try:
            winners.append(
                service.add_authorization(
                    authorization_payload(source_approval_id="approval-race-diff", steward_hash=steward_hash)
                )
            )
        except ConflictError as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("steward-aaaa-00000001",)),
        threading.Thread(target=worker, args=("steward-dddd-00000004",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(winners) == 1
    assert len(errors) == 1
    assert winners[0]["request_outcome"] == "created"
    connection = get_connection()
    rows = connection.execute(
        "SELECT steward_hash FROM steward_authorizations WHERE source_approval_id='approval-race-diff'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["steward_hash"] in {"steward-aaaa-00000001", "steward-dddd-00000004"}
    conflicts = connection.execute(
        "SELECT COUNT(*) FROM authorization_conflicts WHERE source_approval_id='approval-race-diff'"
    ).fetchone()[0]
    assert conflicts == 1


def test_authorization_detail_exposes_lifecycle_and_conflicts(client):
    ensure_temple(client)
    created = client.post(
        "/api/temple/authorizations", json=authorization_payload(source_approval_id="approval-detail")
    )
    authorization_id = created.json()["id"]
    client.post(
        "/api/temple/authorizations",
        json=authorization_payload(source_approval_id="approval-detail", authorization_code="night-duty"),
    )
    client.post(
        f"/api/temple/authorizations/{authorization_id}/suspend",
        json={"actor": "warden", "reason": "核查"},
    )
    detail = client.get(f"/api/temple/authorizations/{authorization_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["temple_code"] == "lingyun-temple"
    assert "***" in body["steward_hint"]
    # 冲突证据中只能看到脱敏值守员提示。
    assert all("***" in item["steward_hint"] for item in body["conflicts"])
    assert [event["event_type"] for event in body["lifecycle_events"]] == ["suspended"]
    assert len(body["conflicts"]) == 1
    assert body["conflicts"][0]["differences"]  # 差异证据可解析
    assert {item["field"] for item in body["conflicts"][0]["differences"]} == {"authorization_code"}


def test_expired_window_authorization_cannot_start_mitigation_after_reaping(client):
    ensure_temple(client)
    created = client.post(
        "/api/temple/authorizations",
        json=authorization_payload(
            source_approval_id="approval-past",
            valid_from="2026-09-01T00:00:00Z",
            valid_until="2026-09-02T00:00:00Z",
        ),
    )
    assert created.status_code == 201
    # 内容重放不会把到期授权重新激活。
    replay = client.post(
        "/api/temple/authorizations",
        json=authorization_payload(
            source_approval_id="approval-past",
            valid_from="2026-09-01T00:00:00Z",
            valid_until="2026-09-02T00:00:00Z",
        ),
    )
    assert replay.json()["state"] == "active"  # 尚未回收，但时间窗已过
    TempleSafetyService(get_connection(), FrozenClock(datetime(2026, 9, 28, tzinfo=UTC))).authorizations.expire_due_authorizations()
    assert get_connection().execute(
        "SELECT state FROM steward_authorizations WHERE source_approval_id='approval-past'"
    ).fetchone()["state"] == "expired"
