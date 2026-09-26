from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class TempleCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=120)
    temple_type: Literal["heritage", "urban", "mountain", "community"]
    timezone: str = Field(default="Asia/Shanghai", min_length=3, max_length=80)
    max_concurrent_mitigation_sessions: int = Field(default=1000, ge=1, le=1_000_000)
    ventilation_capacity: int = Field(default=10_000, ge=1, le=10_000_000)


class HallCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    name: str = Field(min_length=1, max_length=120)
    visit_order: int = Field(ge=0, le=100_000)
    expected_visit_seconds: int = Field(default=180, ge=1, le=86_400)
    ventilation_capacity: int = Field(default=3000, ge=1, le=10_000_000)


class IncenseProfileCreate(BaseModel):
    incense_code: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=1, max_length=120)
    activity_type: Literal["daily", "festival", "ceremony", "memorial", "tour"]
    pm25_target: int = Field(ge=1, le=10_000)
    co_target: float = Field(ge=0, le=1)
    min_supply_airflow: float = Field(ge=0, le=100_000)
    min_exhaust_airflow: float = Field(ge=0, le=100_000)
    default_risk_priority: int = Field(default=50, ge=0, le=100)


class SafetyPolicyCreate(BaseModel):
    rules: dict[str, Any]
    actor: str = Field(min_length=1, max_length=120)


class SafetyPolicyPublish(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    effective_from: str


class AuthorizationCreate(BaseModel):
    steward_hash: str = Field(min_length=16, max_length=128)
    temple_code: str = Field(min_length=2, max_length=64)
    authorization_code: str = Field(min_length=2, max_length=80)
    valid_from: str
    valid_until: str
    source_approval_id: str = Field(min_length=4, max_length=160)


class ExperienceObservationCreate(BaseModel):
    observation_key: str = Field(min_length=6, max_length=160)
    temple_code: str = Field(min_length=2, max_length=64)
    hall_code: str | None = Field(default=None, max_length=64)
    incense_code: str = Field(min_length=2, max_length=80)
    steward_hash: str = Field(min_length=16, max_length=128)
    sensor_class: str = Field(min_length=1, max_length=80)
    visitor_density: float = Field(default=0, ge=0, le=1000)
    pm25_ugm3: float = Field(ge=0, le=1_000_000)
    co_ppm: float = Field(ge=0, le=1)
    supply_airflow: float = Field(ge=0, le=100_000)
    exhaust_airflow: float = Field(ge=0, le=100_000)
    observed_at: str


class MitigationStart(BaseModel):
    actor: str = Field(min_length=1, max_length=120)


class MitigationSessionFinish(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=500)
    result: Literal["completed", "cancelled"] = "completed"


class BatchObservations(BaseModel):
    items: list[ExperienceObservationCreate] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_keys(self) -> "BatchObservations":
        keys = [item.observation_key for item in self.items]
        if len(keys) != len(set(keys)):
            raise ValueError("同一批次内 observation_key 不能重复")
        return self
