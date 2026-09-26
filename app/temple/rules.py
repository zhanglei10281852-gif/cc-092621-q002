from __future__ import annotations

import json
from typing import Any

from app.core.errors import ValidationError
from app.core.security import request_fingerprint
from app.temple.types import Allocation, QualityDecision

DEFAULT_RULES: dict[str, Any] = {
    "score": {
        "pm25_weight": 0.35,
        "co_weight": 0.30,
        "supply_airflow_weight": 0.20,
        "exhaust_airflow_weight": 0.15,
        "major_threshold": 1.5,
        "critical_threshold": 2.5,
    },
    "allocation": {
        "minor_multiplier": 1.15,
        "major_multiplier": 1.50,
        "critical_multiplier": 2.00,
        "duration_seconds": 180,
        "max_supply_airflow": 200.0,
        "max_exhaust_airflow": 50.0,
    },
}


def canonical_rules(rules: dict[str, Any]) -> tuple[str, str]:
    validate_rules(rules)
    text = json.dumps(rules, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text, request_fingerprint(rules)


def validate_rules(rules: dict[str, Any]) -> None:
    score = rules.get("score")
    allocation = rules.get("allocation")
    if not isinstance(score, dict) or not isinstance(allocation, dict):
        raise ValidationError("策略必须同时包含 score 与 allocation")
    weights = [score.get(name) for name in ("pm25_weight", "co_weight", "supply_airflow_weight", "exhaust_airflow_weight")]
    if any(not isinstance(value, (int, float)) or value < 0 for value in weights):
        raise ValidationError("评分权重必须是非负数")
    if abs(sum(float(value) for value in weights) - 1.0) > 0.0001:
        raise ValidationError("评分权重之和必须等于 1")
    major = score.get("major_threshold")
    critical = score.get("critical_threshold")
    if not isinstance(major, (int, float)) or not isinstance(critical, (int, float)) or not 0 < major < critical:
        raise ValidationError("重大和严重阈值必须递增")
    for key in ("minor_multiplier", "major_multiplier", "critical_multiplier"):
        value = allocation.get(key)
        if not isinstance(value, (int, float)) or value < 1:
            raise ValidationError(f"{key} 必须不小于 1")
    duration = allocation.get("duration_seconds")
    if not isinstance(duration, int) or not 30 <= duration <= 3600:
        raise ValidationError("缓解时长必须在 30 到 3600 秒之间")


def judge_quality(observation: dict[str, Any], profile: dict[str, Any], rules: dict[str, Any]) -> QualityDecision:
    score_rules = rules["score"]
    ratios = {
        "pm25": max(0.0, float(observation["pm25_ugm3"]) / float(profile["pm25_target"]) - 1.0),
        "co": max(0.0, float(observation["co_ppm"]) / max(float(profile["co_target"]), 0.0001) - 1.0),
        "supply_airflow": max(0.0, float(profile["min_supply_airflow"]) / max(float(observation["supply_airflow"]), 0.001) - 1.0),
        "exhaust_airflow": max(0.0, float(profile["min_exhaust_airflow"]) / max(float(observation["exhaust_airflow"]), 0.001) - 1.0),
    }
    weights = {
        "pm25": float(score_rules["pm25_weight"]),
        "co": float(score_rules["co_weight"]),
        "supply_airflow": float(score_rules["supply_airflow_weight"]),
        "exhaust_airflow": float(score_rules["exhaust_airflow_weight"]),
    }
    score = round(sum(ratios[key] * weights[key] for key in ratios), 6)
    reasons = tuple(key for key, ratio in ratios.items() if ratio > 0)
    if not reasons:
        return QualityDecision(False, None, (), 0.0)
    if score >= float(score_rules["critical_threshold"]):
        severity = "critical"
    elif score >= float(score_rules["major_threshold"]):
        severity = "major"
    else:
        severity = "minor"
    return QualityDecision(True, severity, reasons, score)


def allocation_for(profile: dict[str, Any], severity: str, rules: dict[str, Any]) -> Allocation:
    allocation = rules["allocation"]
    multiplier = float(allocation[f"{severity}_multiplier"])
    supply_airflow = min(float(profile["min_supply_airflow"]) * multiplier, float(allocation["max_supply_airflow"]))
    exhaust_airflow = min(float(profile["min_exhaust_airflow"]) * multiplier, float(allocation["max_exhaust_airflow"]))
    priority_bonus = {"minor": 5, "major": 15, "critical": 25}[severity]
    priority = min(100, int(profile["default_risk_priority"]) + priority_bonus)
    return Allocation(round(supply_airflow, 3), round(exhaust_airflow, 3), priority, int(allocation["duration_seconds"]))
