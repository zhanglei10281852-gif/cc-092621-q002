from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.clock import FrozenClock, to_storage
from app.database import get_connection
from app.temple.operations import TempleRestorationService
from app.temple.rules import DEFAULT_RULES


def prepare(client):
    client.post(
        "/api/temple/temples",
        json={"code": "shanmen-temple", "name": "山门古寺", "temple_type": "mountain", "timezone": "Asia/Shanghai", "max_concurrent_mitigation_sessions": 100, "ventilation_capacity": 1000},
    )
    for sequence, code in enumerate(("east", "west"), start=1):
        client.post(
            "/api/temple/temples/shanmen-temple/halls",
            json={"code": code, "name": f"{code}-hall", "visit_order": sequence, "expected_visit_seconds": 600, "ventilation_capacity": 400},
        )
    client.post(
        "/api/temple/incense_profiles",
        json={"incense_code": "ceremony-incense", "name": "法会香火", "activity_type": "ceremony", "pm25_target": 120, "co_target": 0.02, "min_supply_airflow": 10, "min_exhaust_airflow": 8, "default_risk_priority": 75},
    )
    safety_policy = client.post("/api/temple/temples/shanmen-temple/policies", json={"rules": DEFAULT_RULES, "actor": "tests"}).json()
    client.post(f"/api/temple/policies/{safety_policy['id']}/publish", json={"actor": "tests", "effective_from": "2026-09-26T00:00:00Z"})
    return safety_policy


def test_rollout_restoration_campaign_lifecycle(client):
    safety_policy = prepare(client)
    created = client.post(
        "/api/temple/operations/restoration_campaigns",
        json={
            "temple_code": "shanmen-temple",
            "safety_policy_id": safety_policy["id"],
            "code": "venue-evening-rollout",
            "name": "晚间场馆发布",
            "strategy": "halls",
            "target_percentage": 100,
            "hall_codes": ["east", "west"],
            "cohort_keys": ["standard", "premium"],
            "actor": "operator",
        },
    )
    assert created.status_code == 201, created.text
    assert len(created.json()["targets"]) == 4
    restoration_campaign_id = created.json()["id"]
    started = client.post(f"/api/temple/operations/restoration_campaigns/{restoration_campaign_id}/start", json={"actor": "operator", "reason": "进入发布窗口"})
    assert started.status_code == 200
    assert {item["state"] for item in started.json()["targets"]} == {"active"}
    paused = client.post(f"/api/temple/operations/restoration_campaigns/{restoration_campaign_id}/pause", json={"actor": "operator", "reason": "观察错误率"})
    assert paused.status_code == 200
    assert paused.json()["state"] == "paused"
    resumed = client.post(f"/api/temple/operations/restoration_campaigns/{restoration_campaign_id}/start", json={"actor": "operator", "reason": "指标稳定"})
    assert resumed.status_code == 200
    completed = client.post(f"/api/temple/operations/restoration_campaigns/{restoration_campaign_id}/complete", json={"actor": "operator", "reason": "发布完成"})
    assert completed.status_code == 200
    assert completed.json()["state"] == "completed"
    assert [event["event_type"] for event in completed.json()["events"]] == ["created", "started", "paused", "started", "completed"]


def test_restoration_campaign_validates_safety_policy_and_targets(client):
    safety_policy = prepare(client)
    missing_hall = client.post(
        "/api/temple/operations/restoration_campaigns",
        json={"temple_code": "shanmen-temple", "safety_policy_id": safety_policy["id"], "code": "bad-target", "name": "错误殿堂", "strategy": "halls", "hall_codes": ["missing"], "actor": "operator"},
    )
    assert missing_hall.status_code == 404
    duplicate = {
        "temple_code": "shanmen-temple", "safety_policy_id": safety_policy["id"], "code": "same-code", "name": "同编码活动", "strategy": "phased", "target_percentage": 20, "actor": "operator"
    }
    assert client.post("/api/temple/operations/restoration_campaigns", json=duplicate).status_code == 201
    assert client.post("/api/temple/operations/restoration_campaigns", json=duplicate).status_code == 409


def test_closure_overlap_and_scheduler(client):
    prepare(client)
    now = datetime(2026, 9, 26, 8, 0, tzinfo=UTC)
    service = TempleRestorationService(get_connection(), FrozenClock(now))
    first = service.create_closure({
        "temple_code": "shanmen-temple", "hall_code": "east", "code": "east-closure", "reason": "排风设备检修", "starts_at": to_storage(now + timedelta(minutes=10)), "ends_at": to_storage(now + timedelta(minutes=40)), "drain_mode": "block_new", "actor": "operator"
    })
    assert first["state"] == "scheduled"
    try:
        service.create_closure({
            "temple_code": "shanmen-temple", "hall_code": "east", "code": "overlap", "reason": "重叠维护", "starts_at": to_storage(now + timedelta(minutes=20)), "ends_at": to_storage(now + timedelta(minutes=50)), "drain_mode": "finish_active", "actor": "operator"
        })
    except Exception as exc:
        assert getattr(exc, "code", "") == "conflict"
    else:
        raise AssertionError("重叠维护窗口应被拒绝")
    advanced = TempleRestorationService(get_connection(), FrozenClock(now + timedelta(minutes=15))).activate_due_closure("scheduler")
    assert first["id"] in advanced["activated"]
    blocked = service.blocks_new_mitigation_session(first["temple_id"], first["hall_id"], to_storage(now + timedelta(minutes=15)))
    assert blocked and blocked["code"] == "east-closure"
    completed = TempleRestorationService(get_connection(), FrozenClock(now + timedelta(minutes=45))).activate_due_closure("scheduler")
    assert first["id"] in completed["completed"]


def test_closure_blocks_mitigation(client):
    safety_policy = prepare(client)
    client.post(
        "/api/temple/authorizations",
        json={"steward_hash": "steward-closure-01", "temple_code": "shanmen-temple", "authorization_code": "ceremony-duty", "valid_from": "2026-09-26T00:00:00Z", "valid_until": "2026-09-27T00:00:00Z", "source_approval_id": "closure-order"},
    )
    observation = client.post(
        "/api/temple/observations",
        json={"observation_key": "closure-observation", "temple_code": "shanmen-temple", "hall_code": "east", "incense_code": "ceremony-incense", "steward_hash": "steward-closure-01", "sensor_class": "ceiling-sensor", "visitor_density": 0, "pm25_ugm3": 500, "co_ppm": 0.2, "supply_airflow": 1, "exhaust_airflow": 0.2, "observed_at": "2026-09-26T05:30:00Z"},
    ).json()
    created = client.post(
        "/api/temple/operations/closure",
        json={"temple_code": "shanmen-temple", "hall_code": "east", "code": "active-closure", "reason": "梁架检查", "starts_at": "2020-01-01T00:00:00Z", "ends_at": "2030-01-01T00:00:00Z", "drain_mode": "block_new", "actor": "operator"},
    )
    assert created.status_code == 201
    denied = client.post(f"/api/temple/safety_incidents/{observation['safety_incident_id']}/mitigate", json={"actor": "operator"})
    assert denied.status_code == 409
    assert denied.json()["error"]["context"]["closure_code"] == "active-closure"
