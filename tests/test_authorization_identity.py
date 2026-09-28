from __future__ import annotations

import threading

import pytest

from app.database import get_connection
from app.temple.service import TempleSafetyService


def create_temple(client, code="lingyun-temple", name="凌云古寺"):
    response = client.post(
        "/api/temple/temples",
        json={
            "code": code,
            "name": name,
            "temple_type": "heritage",
            "timezone": "Asia/Shanghai",
            "max_concurrent_mitigation_sessions": 10,
            "ventilation_capacity": 3000,
        },
    )
    assert response.status_code == 201, response.text


def authorization_payload(**overrides):
    payload = {
        "steward_hash": "steward-aaaaaaaa-0001",
        "temple_code": "lingyun-temple",
        "authorization_code": "festival-duty",
        "valid_from": "2030-01-01T00:00:00Z",
        "valid_until": "2030-12-31T00:00:00Z",
        "source_approval_id": "approval-immutable-0001",
    }
    payload.update(overrides)
    return payload


def audit_events(connection, approval_id):
    rows = connection.execute(
        "SELECT event_type,state FROM authorization_audit_events WHERE source_approval_id=? ORDER BY id",
        (approval_id,),
    ).fetchall()
    return [(row["event_type"], row["state"]) for row in rows]


def test_first_registration_is_created(client):
    create_temple(client)
    response = client.post("/api/temple/authorizations", json=authorization_payload())
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["registration"] == "created"
    assert body["source_approval_id"] == "approval-immutable-0001"
    assert body["state"] == "active"
    assert body["content_digest"]
    assert audit_events(get_connection(), "approval-immutable-0001") == [("created", "active")]


def test_identical_replay_returns_original_record_with_200(client):
    create_temple(client)
    first = client.post("/api/temple/authorizations", json=authorization_payload())
    second = client.post("/api/temple/authorizations", json=authorization_payload())
    assert first.status_code == 201
    assert second.status_code == 200, second.text
    assert second.json()["registration"] == "replayed"
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["content_digest"] == first.json()["content_digest"]
    connection = get_connection()
    assert connection.execute(
        "SELECT COUNT(*) FROM steward_authorizations WHERE source_approval_id=?",
        ("approval-immutable-0001",),
    ).fetchone()[0] == 1
    assert audit_events(connection, "approval-immutable-0001") == [("created", "active"), ("replayed", "active")]


@pytest.mark.parametrize(
    "field,new_value,expected_field",
    [
        ("steward_hash", "steward-bbbbbbbb-0002", "steward_hash"),
        ("authorization_code", "ceremony-duty", "authorization_code"),
        ("valid_from", "2030-02-01T00:00:00Z", "valid_from"),
        ("valid_until", "2031-01-01T00:00:00Z", "valid_until"),
    ],
)
def test_any_content_difference_is_rejected_with_masked_summary(client, field, new_value, expected_field):
    create_temple(client)
    create_temple(client, code="baoxiang-temple", name="宝象古寺")
    client.post("/api/temple/authorizations", json=authorization_payload())
    conflict = client.post(
        "/api/temple/authorizations",
        json=authorization_payload(**{field: new_value}),
    )
    assert conflict.status_code == 409, conflict.text
    error = conflict.json()["error"]
    assert error["code"] == "conflict"
    assert error["context"]["source_approval_id"] == "approval-immutable-0001"
    differences = error["context"]["differences"]
    assert [item["field"] for item in differences] == [expected_field]
    # 差异摘要不得暴露完整人员标识
    full_hash = authorization_payload()["steward_hash"]
    assert full_hash not in conflict.text
    assert all(not item["existing"].startswith("steward-aaaaaaaa") for item in differences)
    connection = get_connection()
    assert connection.execute(
        "SELECT COUNT(*) FROM steward_authorizations WHERE source_approval_id=?",
        ("approval-immutable-0001",),
    ).fetchone()[0] == 1
    events = audit_events(connection, "approval-immutable-0001")
    assert events == [("created", "active"), ("content_conflict", "active")]


def test_different_temple_is_conflict(client):
    create_temple(client)
    create_temple(client, code="baoxiang-temple", name="宝象古寺")
    client.post("/api/temple/authorizations", json=authorization_payload())
    conflict = client.post(
        "/api/temple/authorizations",
        json=authorization_payload(temple_code="baoxiang-temple"),
    )
    assert conflict.status_code == 409
    differences = conflict.json()["error"]["context"]["differences"]
    assert [item["field"] for item in differences] == ["temple_code"]
    assert differences[0]["existing"] == "lingyun-temple"
    assert differences[0]["incoming"] == "baoxiang-temple"


@pytest.mark.parametrize("state", ["suspended", "cancelled", "expired"])
def test_replay_does_not_revoke_or_change_inactive_state(client, state):
    create_temple(client)
    created = client.post("/api/temple/authorizations", json=authorization_payload()).json()
    connection = get_connection()
    connection.execute(
        "UPDATE steward_authorizations SET state=? WHERE id=?",
        (state, created["id"]),
    )
    replayed = client.post("/api/temple/authorizations", json=authorization_payload())
    assert replayed.status_code == 200
    assert replayed.json()["state"] == state
    stored = connection.execute(
        "SELECT state FROM steward_authorizations WHERE source_approval_id=?",
        ("approval-immutable-0001",),
    ).fetchone()
    assert stored["state"] == state
    assert audit_events(connection, "approval-immutable-0001") == [
        ("created", "active"),
        ("state_replay_blocked", state),
    ]


def test_conflicting_replay_against_inactive_authorization_is_rejected(client):
    create_temple(client)
    client.post("/api/temple/authorizations", json=authorization_payload())
    connection = get_connection()
    connection.execute(
        "UPDATE steward_authorizations SET state='cancelled' WHERE source_approval_id=?",
        ("approval-immutable-0001",),
    )
    conflict = client.post(
        "/api/temple/authorizations",
        json=authorization_payload(authorization_code="tampered-duty"),
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["context"]["existing_state"] == "cancelled"
    assert connection.execute(
        "SELECT state FROM steward_authorizations WHERE source_approval_id=?",
        ("approval-immutable-0001",),
    ).fetchone()["state"] == "cancelled"


def test_concurrent_identical_requests_leave_single_version(client):
    create_temple(client)
    barrier = threading.Barrier(2)
    outcomes: list[tuple[int, str]] = []
    lock = threading.Lock()

    def worker():
        service = TempleSafetyService()
        barrier.wait()
        result = service.add_authorization(authorization_payload(), actor="upstream-thread")
        with lock:
            outcomes.append((200 if result.outcome == "replayed" else 201, result.outcome))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == [(200, "replayed"), (201, "created")]
    connection = get_connection()
    assert connection.execute(
        "SELECT COUNT(*) FROM steward_authorizations WHERE source_approval_id=?",
        ("approval-immutable-0001",),
    ).fetchone()[0] == 1


def test_concurrent_conflicting_requests_leave_single_version_and_audit_conflict(client):
    create_temple(client)
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}
    lock = threading.Lock()

    def worker(name, code):
        service = TempleSafetyService()
        barrier.wait()
        try:
            result = service.add_authorization(
                authorization_payload(authorization_code=code), actor=f"upstream-{name}"
            )
            with lock:
                results[name] = ("created", result.body["id"])
        except Exception as exc:  # noqa: BLE001 - 并发双方一方必然冲突
            with lock:
                results[name] = ("conflict", exc.__class__.__name__)

    threads = [
        threading.Thread(target=worker, args=("a", "festival-duty")),
        threading.Thread(target=worker, args=("b", "ceremony-duty")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(value[0] for value in results.values()) == ["conflict", "created"]
    connection = get_connection()
    rows = connection.execute(
        "SELECT id FROM steward_authorizations WHERE source_approval_id=?",
        ("approval-immutable-0001",),
    ).fetchall()
    assert len(rows) == 1
    event_types = [event[0] for event in audit_events(connection, "approval-immutable-0001")]
    assert event_types == ["created", "content_conflict"]


def test_legacy_row_without_digest_is_backfilled_on_replay(client):
    create_temple(client)
    connection = get_connection()
    connection.execute(
        "INSERT INTO steward_authorizations(steward_hash,temple_id,authorization_code,valid_from,valid_until,"
        "source_approval_id,content_digest,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "steward-aaaaaaaa-0001",
            connection.execute("SELECT id FROM temple_sites WHERE code='lingyun-temple'").fetchone()[0],
            "festival-duty",
            "2030-01-01T00:00:00+00:00",
            "2030-12-31T00:00:00+00:00",
            "approval-legacy-0001",
            "",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    response = client.post(
        "/api/temple/authorizations",
        json=authorization_payload(source_approval_id="approval-legacy-0001"),
    )
    assert response.status_code == 200
    assert response.json()["registration"] == "replayed"
    stored = connection.execute(
        "SELECT content_digest FROM steward_authorizations WHERE source_approval_id=?",
        ("approval-legacy-0001",),
    ).fetchone()
    assert stored["content_digest"] == response.json()["content_digest"]


def test_audit_endpoint_distinguishes_outcomes(client):
    create_temple(client)
    client.post("/api/temple/authorizations", json=authorization_payload())
    client.post("/api/temple/authorizations", json=authorization_payload())
    client.post(
        "/api/temple/authorizations",
        json=authorization_payload(valid_until="2031-06-30T00:00:00Z"),
    )
    response = client.get(
        "/api/temple/authorizations/audit",
        params={"source_approval_id": "approval-immutable-0001"},
    )
    assert response.status_code == 200, response.text
    events = response.json()["items"]
    # 最新在前
    assert [event["event_type"] for event in events] == ["content_conflict", "replayed", "created"]
    conflict_event = events[0]
    assert conflict_event["differences"][0]["field"] == "valid_until"
