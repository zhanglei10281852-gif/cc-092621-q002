from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.clock import FrozenClock, to_storage
from app.database import get_connection
from app.temple.rules import DEFAULT_RULES, allocation_for, judge_quality
from app.temple.service import TempleSafetyService


def temple_payload(**overrides):
    payload = {
        "code": "lingyun-temple",
        "name": "凌云古寺",
        "temple_type": "heritage",
        "timezone": "Asia/Shanghai",
        "max_concurrent_mitigation_sessions": 10,
        "ventilation_capacity": 3000,
    }
    payload.update(overrides)
    return payload


def app_payload(**overrides):
    payload = {
        "incense_code": "festival-incense",
        "name": "节庆香火",
        "activity_type": "festival",
        "pm25_target": 100,
        "co_target": 0.01,
        "min_supply_airflow": 8,
        "min_exhaust_airflow": 4,
        "default_risk_priority": 70,
    }
    payload.update(overrides)
    return payload


def observation_payload(**overrides):
    payload = {
        "observation_key": "observation-000001",
        "temple_code": "lingyun-temple",
        "hall_code": "main-hall",
        "incense_code": "festival-incense",
        "steward_hash": "steward-000000000001",
        "sensor_class": "ceiling-sensor",
        "visitor_density": 300,
        "pm25_ugm3": 350,
        "co_ppm": 0.08,
        "supply_airflow": 1.5,
        "exhaust_airflow": 0.5,
        "observed_at": "2026-09-26T05:30:00Z",
    }
    payload.update(overrides)
    return payload


def prepare(client):
    temple = client.post("/api/temple/temples", json=temple_payload())
    assert temple.status_code == 201, temple.text
    hall = client.post(
        "/api/temple/temples/lingyun-temple/halls",
        json={"code": "main-hall", "name": "大雄宝殿", "visit_order": 1, "expected_visit_seconds": 900, "ventilation_capacity": 1200},
    )
    assert hall.status_code == 201, hall.text
    app = client.post("/api/temple/incense_profiles", json=app_payload())
    assert app.status_code == 201, app.text
    safety_policy = client.post("/api/temple/temples/lingyun-temple/policies", json={"rules": DEFAULT_RULES, "actor": "tests"})
    assert safety_policy.status_code == 201, safety_policy.text
    published = client.post(
        f"/api/temple/policies/{safety_policy.json()['id']}/publish",
        json={"actor": "tests", "effective_from": "2026-09-26T00:00:00Z"},
    )
    assert published.status_code == 200, published.text
    return {"temple": temple.json(), "app": app.json(), "safety_policy": published.json()}


def test_quality_rule_engine_is_deterministic():
    profile = app_payload()
    healthy = judge_quality(observation_payload(pm25_ugm3=80, co_ppm=0.005, supply_airflow=20, exhaust_airflow=8), profile, DEFAULT_RULES)
    assert healthy.degraded is False
    degraded = judge_quality(observation_payload(), profile, DEFAULT_RULES)
    assert degraded.degraded is True
    assert degraded.severity in {"major", "critical"}
    assert set(degraded.reasons) == {"pm25", "co", "supply_airflow", "exhaust_airflow"}
    allocation = allocation_for(profile, degraded.severity, DEFAULT_RULES)
    assert allocation.supply_airflow >= profile["min_supply_airflow"]
    assert allocation.priority > profile["default_risk_priority"]


def test_temple_app_safety_policy_and_idempotent_observation(client):
    prepare(client)
    first = client.post("/api/temple/observations", json=observation_payload())
    assert first.status_code == 202, first.text
    assert first.json()["safety_incident_id"] is not None
    duplicate = client.post("/api/temple/observations", json=observation_payload())
    assert duplicate.status_code == 202
    assert duplicate.json()["observation_id"] == first.json()["observation_id"]
    conflict = client.post("/api/temple/observations", json=observation_payload(pm25_ugm3=999))
    assert conflict.status_code == 409


def test_mitigation_requires_authorization_and_releases_ventilation(client):
    prepare(client)
    observation = client.post("/api/temple/observations", json=observation_payload()).json()
    denied = client.post(f"/api/temple/safety_incidents/{observation['safety_incident_id']}/mitigate", json={"actor": "tests"})
    assert denied.status_code == 409
    authorization = client.post(
        "/api/temple/authorizations",
        json={
            "steward_hash": observation_payload()["steward_hash"],
            "temple_code": "lingyun-temple",
            "authorization_code": "festival-duty",
            "valid_from": "2026-09-26T00:00:00Z",
            "valid_until": "2026-09-27T00:00:00Z",
            "source_approval_id": "order-000001",
        },
    )
    assert authorization.status_code == 201
    started = client.post(f"/api/temple/safety_incidents/{observation['safety_incident_id']}/mitigate", json={"actor": "tests"})
    assert started.status_code == 200, started.text
    repeated = client.post(f"/api/temple/safety_incidents/{observation['safety_incident_id']}/mitigate", json={"actor": "tests"})
    assert repeated.status_code == 200
    assert repeated.json()["id"] == started.json()["id"]
    finished = client.post(
        f"/api/temple/mitigation_sessions/{started.json()['id']}/finish",
        json={"actor": "tests", "reason": "体验恢复", "result": "completed"},
    )
    assert finished.status_code == 200
    assert finished.json()["status"] == "completed"
    assert finished.json()["reservation"]["state"] == "released"
    assert [event["event_type"] for event in finished.json()["events"]] == ["started", "completed"]


def test_expired_mitigation_session_reopens_safety_incident_with_fixed_clock(client):
    prepare(client)
    client.post(
        "/api/temple/authorizations",
        json={
            "steward_hash": observation_payload()["steward_hash"],
            "temple_code": "lingyun-temple",
            "authorization_code": "festival-duty",
            "valid_from": "2026-09-26T00:00:00Z",
            "valid_until": "2026-09-27T00:00:00Z",
            "source_approval_id": "order-000002",
        },
    )
    observation = client.post("/api/temple/observations", json=observation_payload(observation_key="observation-000002")).json()
    started = client.post(f"/api/temple/safety_incidents/{observation['safety_incident_id']}/mitigate", json={"actor": "tests"}).json()
    connection = get_connection()
    expiry = datetime.fromisoformat(started["expires_at"].replace("Z", "+00:00"))
    service = TempleSafetyService(connection, FrozenClock(expiry + timedelta(seconds=1)))
    result = service.expire_mitigation_sessions("tests")
    assert started["id"] in result["expired"]
    detail = service.get_mitigation_session(started["id"])
    assert detail["status"] == "expired"
    assert detail["reservation"]["state"] == "released"
    safety_incident = connection.execute("SELECT state FROM safety_incidents WHERE id=?", (observation["safety_incident_id"],)).fetchone()
    assert safety_incident["state"] == "open"


def test_ventilation_limit_rejects_second_mitigation_session(client):
    prepare(client)
    connection = get_connection()
    connection.execute("UPDATE worship_halls SET ventilation_capacity=20 WHERE code='main-hall'")
    for index in (1, 2):
        steward = f"steward-{index:018d}"
        client.post(
            "/api/temple/authorizations",
            json={
                "steward_hash": steward,
                "temple_code": "lingyun-temple",
                "authorization_code": "festival-duty",
                "valid_from": "2026-09-26T00:00:00Z",
                "valid_until": "2026-09-27T00:00:00Z",
                "source_approval_id": f"order-ventilation-{index:03d}",
            },
        )
        observation = client.post("/api/temple/observations", json=observation_payload(observation_key=f"observation-ventilation-{index:03d}", steward_hash=steward)).json()
        response = client.post(f"/api/temple/safety_incidents/{observation['safety_incident_id']}/mitigate", json={"actor": "tests"})
        if index == 1:
            assert response.status_code == 200
        else:
            assert response.status_code == 409


def test_safety_policy_versions_replace_previous_publication(client):
    prepared = prepare(client)
    changed = {**DEFAULT_RULES, "allocation": {**DEFAULT_RULES["allocation"], "duration_seconds": 240}}
    draft = client.post("/api/temple/temples/lingyun-temple/policies", json={"rules": changed, "actor": "tests"})
    assert draft.status_code == 201
    publish = client.post(
        f"/api/temple/policies/{draft.json()['id']}/publish",
        json={"actor": "tests", "effective_from": "2026-09-26T01:00:00Z"},
    )
    assert publish.status_code == 200
    old = get_connection().execute("SELECT state FROM safety_policy_versions WHERE id=?", (prepared["safety_policy"]["id"],)).fetchone()
    assert old["state"] == "retired"


def test_demo_seed_and_summary(client):
    seeded = client.post("/api/temple/demo/seed")
    assert seeded.status_code == 200
    repeated = client.post("/api/temple/demo/seed")
    assert repeated.status_code == 200
    summary = client.get("/api/temple/summary")
    assert summary.status_code == 200
    assert summary.json()["temples"]["active"] == 1
