from __future__ import annotations

from fastapi import APIRouter, Query

from app.database import get_connection
from app.temple.analytics import TempleAnalytics, ReportWindow
from app.temple.schemas import MitigationStart, IncenseProfileCreate, BatchObservations, AuthorizationCreate, ExperienceObservationCreate, SafetyPolicyCreate, SafetyPolicyPublish, TempleCreate, HallCreate, MitigationSessionFinish
from app.temple.service import TempleSafetyService

router = APIRouter(prefix="/api/temple", tags=["寺院香火安全与修缮协同"])


def service() -> TempleSafetyService:
    return TempleSafetyService()


@router.post("/temples", status_code=201)
def create_temple(payload: TempleCreate):
    return service().create_temple(payload.model_dump())


@router.get("/temples")
def list_temples(status: str | None = None):
    return {"items": service().list_temples(status)}


@router.post("/temples/{temple_code}/halls", status_code=201)
def add_hall(temple_code: str, payload: HallCreate):
    return service().add_hall(temple_code, payload.model_dump())


@router.post("/incense_profiles", status_code=201)
def create_incense_profile(payload: IncenseProfileCreate):
    return service().create_incense_profile(payload.model_dump())


@router.get("/incense_profiles")
def list_incense_profiles(activity_type: str | None = None):
    return {"items": service().list_incense_profiles(activity_type)}


@router.post("/temples/{temple_code}/policies", status_code=201)
def create_safety_policy(temple_code: str, payload: SafetyPolicyCreate):
    return service().create_safety_policy(temple_code, payload.rules, payload.actor)


@router.post("/policies/{safety_policy_id}/publish")
def publish_safety_policy(safety_policy_id: int, payload: SafetyPolicyPublish):
    return service().publish_safety_policy(safety_policy_id, payload.actor, payload.effective_from)


@router.post("/authorizations", status_code=201)
def add_authorization(payload: AuthorizationCreate):
    return service().add_authorization(payload.model_dump())


@router.post("/observations", status_code=202)
def ingest_observation(payload: ExperienceObservationCreate):
    return service().ingest_observation(payload.model_dump())


@router.post("/observations/batch", status_code=202)
def ingest_batch(payload: BatchObservations):
    return service().ingest_batch([item.model_dump() for item in payload.items])


@router.get("/safety_incidents")
def open_safety_incidents(temple_code: str | None = None, limit: int = Query(default=100, ge=1, le=500)):
    return {"items": service().open_safety_incidents(temple_code, limit)}


@router.post("/safety_incidents/{safety_incident_id}/mitigate")
def start_mitigation(safety_incident_id: int, payload: MitigationStart):
    return service().start_mitigation(safety_incident_id, payload.actor)


@router.get("/mitigation_sessions/{mitigation_session_id}")
def get_mitigation_session(mitigation_session_id: int):
    return service().get_mitigation_session(mitigation_session_id)


@router.post("/mitigation_sessions/{mitigation_session_id}/finish")
def finish_mitigation_session(mitigation_session_id: int, payload: MitigationSessionFinish):
    return service().finish_mitigation_session(mitigation_session_id, payload.actor, payload.reason, payload.result)


@router.post("/mitigation_sessions/expire")
def expire_mitigation_sessions(actor: str = Query(default="mitigation_session-reaper", min_length=1)):
    return service().expire_mitigation_sessions(actor)


@router.get("/summary")
def summary():
    return service().summary()


@router.get("/analytics/quality")
def quality_report(started_at: str | None = None, ended_at: str | None = None):
    analytics = TempleAnalytics(get_connection())
    window = ReportWindow(started_at, ended_at)
    return {
        "overview": analytics.quality_overview(window),
        "temples": analytics.temple_breakdown(window),
        "incense_profiles": analytics.incense_profile_breakdown(window),
        "halls": analytics.hall_breakdown(None, window),
        "severity": analytics.severity_distribution(None, window),
    }


@router.get("/analytics/ventilation")
def ventilation_report():
    return {"items": TempleAnalytics(get_connection()).ventilation_snapshot()}


@router.get("/analytics/outcomes")
def outcome_report(started_at: str | None = None, ended_at: str | None = None):
    return TempleAnalytics(get_connection()).mitigation_outcomes(ReportWindow(started_at, ended_at))


@router.get("/events")
def event_timeline(after_id: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500)):
    return TempleAnalytics(get_connection()).event_timeline(after_id=after_id, limit=limit)


@router.get("/analytics/halls")
def hall_report(temple_code: str | None = None, started_at: str | None = None, ended_at: str | None = None):
    return {"items": TempleAnalytics(get_connection()).hall_breakdown(temple_code, ReportWindow(started_at, ended_at))}


@router.get("/analytics/stale-safety_incidents")
def stale_safety_incidents(before: str, limit: int = Query(default=100, ge=1, le=500)):
    return {"items": TempleAnalytics(get_connection()).stale_open_safety_incidents(before, limit=limit)}


@router.post("/demo/seed")
def seed_demo():
    return service().seed_demo()
