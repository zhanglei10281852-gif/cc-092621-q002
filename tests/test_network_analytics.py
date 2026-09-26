from __future__ import annotations

from app.database import get_connection
from app.temple.analytics import TempleAnalytics, ReportWindow
from app.temple.rules import DEFAULT_RULES


def seed(client):
    client.post(
        "/api/temple/temples",
        json={"code": "city-temple", "name": "慈云寺", "temple_type": "urban", "timezone": "Asia/Shanghai", "max_concurrent_mitigation_sessions": 100, "ventilation_capacity": 1000},
    )
    client.post(
        "/api/temple/temples/city-temple/halls",
        json={"code": "guanyin-hall", "name": "观音殿", "visit_order": 1, "expected_visit_seconds": 120, "ventilation_capacity": 300},
    )
    client.post(
        "/api/temple/incense_profiles",
        json={"incense_code": "daily-incense", "name": "日常香火", "activity_type": "daily", "pm25_target": 60, "co_target": 0.01, "min_supply_airflow": 5, "min_exhaust_airflow": 2, "default_risk_priority": 80},
    )
    safety_policy = client.post("/api/temple/temples/city-temple/policies", json={"rules": DEFAULT_RULES, "actor": "analytics"}).json()
    client.post(f"/api/temple/policies/{safety_policy['id']}/publish", json={"actor": "analytics", "effective_from": "2026-09-26T00:00:00Z"})


def test_empty_reports(client):
    analytics = TempleAnalytics(get_connection())
    assert analytics.quality_overview()["observations"] == 0
    assert analytics.mitigation_outcomes()["total"] == 0
    assert analytics.event_timeline() == {"items": [], "next_cursor": 0, "has_more": False}


def test_quality_and_ventilation_reports(client):
    seed(client)
    healthy = {
        "observation_key": "analytics-healthy",
        "temple_code": "city-temple",
        "hall_code": "guanyin-hall",
        "incense_code": "daily-incense",
        "steward_hash": "steward-analytics-0001",
        "sensor_class": "wall-sensor",
        "visitor_density": 60,
        "pm25_ugm3": 40,
        "co_ppm": 0.002,
        "supply_airflow": 20,
        "exhaust_airflow": 5,
        "observed_at": "2026-09-26T02:00:00Z",
    }
    degraded = {**healthy, "observation_key": "analytics-degraded", "steward_hash": "steward-analytics-0002", "pm25_ugm3": 300, "co_ppm": 0.1, "supply_airflow": 1, "exhaust_airflow": 0.2}
    assert client.post("/api/temple/observations", json=healthy).status_code == 202
    assert client.post("/api/temple/observations", json=degraded).status_code == 202
    report = client.get("/api/temple/analytics/quality")
    assert report.status_code == 200
    assert report.json()["overview"]["observations"] == 2
    assert report.json()["overview"]["safety_incidents"] == 1
    assert report.json()["overview"]["degraded_ratio"] == 0.5
    assert report.json()["halls"][0]["safety_incidents"] == 1
    assert report.json()["severity"]["total"] == 1
    ventilation = client.get("/api/temple/analytics/ventilation")
    assert ventilation.status_code == 200
    assert ventilation.json()["items"][0]["available_supply_airflow"] == 300


def test_report_window_and_event_cursor(client):
    seed(client)
    analytics = TempleAnalytics(get_connection())
    window = ReportWindow("2026-09-26T00:00:00Z", "2026-09-27T00:00:00Z")
    assert analytics.quality_overview(window)["observations"] == 0
    connection = get_connection()
    temple = connection.execute("SELECT id FROM temple_sites WHERE code='city-temple'").fetchone()[0]
    hall = connection.execute("SELECT id FROM worship_halls WHERE temple_id=?", (temple,)).fetchone()[0]
    app = connection.execute("SELECT id FROM incense_profiles WHERE incense_code='daily-incense'").fetchone()[0]
    safety_policy = connection.execute("SELECT id FROM safety_policy_versions WHERE temple_id=?", (temple,)).fetchone()[0]
    connection.execute(
        "INSERT INTO incense_observations(observation_key,temple_id,hall_id,incense_profile_id,steward_hash,sensor_class,visitor_density,pm25_ugm3,co_ppm,supply_airflow,exhaust_airflow,observed_at,received_at,payload_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("analytics-manual", temple, hall, app, "steward-analytics-0003", "wall-sensor", 60, 300, 0.1, 1, 0.2, "2026-09-26T02:00:00Z", "2026-09-26T02:00:00Z", "manual-digest"),
    )
    observation_id = connection.execute("SELECT id FROM incense_observations WHERE observation_key='analytics-manual'").fetchone()[0]
    connection.execute(
        "INSERT INTO safety_incidents(observation_id,temple_id,hall_id,incense_profile_id,severity,reasons_json,opened_at) VALUES(?,?,?,?, 'major','{}','2026-09-26T02:00:00Z')",
        (observation_id, temple, hall, app),
    )
    safety_incident = connection.execute("SELECT id FROM safety_incidents WHERE observation_id=?", (observation_id,)).fetchone()[0]
    connection.execute(
        "INSERT INTO mitigation_sessions(safety_incident_id,steward_hash,incense_profile_id,temple_id,hall_id,safety_policy_version_id,allocated_supply_airflow,allocated_exhaust_airflow,priority,started_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (safety_incident, "steward-analytics-0003", app, temple, None, safety_policy, 10, 4, 90, "2026-09-26T02:00:00Z", "2026-09-26T02:03:00Z"),
    )
    mitigation_session = connection.execute("SELECT id FROM mitigation_sessions WHERE safety_incident_id=?", (safety_incident,)).fetchone()[0]
    for event_type in ("started", "ventilation-held", "completed"):
        connection.execute(
            "INSERT INTO mitigation_events(mitigation_session_id,event_type,actor,detail_json,created_at) VALUES(?,?,?,?,?)",
            (mitigation_session, event_type, "tests", "{}", "2026-09-26T02:00:00Z"),
        )
    first = analytics.event_timeline(limit=2)
    assert len(first["items"]) == 2
    assert first["has_more"] is True
    second = analytics.event_timeline(after_id=first["next_cursor"], limit=2)
    assert [item["event_type"] for item in second["items"]] == ["completed"]
    stale = analytics.stale_open_safety_incidents("2026-09-27T00:00:00Z")
    assert stale[0]["id"] == safety_incident
