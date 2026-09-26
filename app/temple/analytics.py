from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.core.errors import ValidationError


@dataclass(frozen=True, slots=True)
class ReportWindow:
    started_at: str | None = None
    ended_at: str | None = None

    def clauses(self, column: str) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if self.started_at:
            clauses.append(f"{column}>=?")
            params.append(self.started_at)
        if self.ended_at:
            clauses.append(f"{column}<?")
            params.append(self.ended_at)
        return clauses, params


class TempleAnalytics:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def quality_overview(self, window: ReportWindow | None = None) -> dict[str, Any]:
        window = window or ReportWindow()
        clauses, params = window.clauses("s.observed_at")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        row = self.connection.execute(
            "SELECT COUNT(*) AS observations,COUNT(i.id) AS safety_incidents,"
            "COALESCE(AVG(s.pm25_ugm3),0) AS average_pm25_ugm3,"
            "COALESCE(AVG(s.co_ppm),0) AS average_co_ppm,"
            "COALESCE(AVG(s.supply_airflow),0) AS average_supply_airflow "
            "FROM incense_observations s LEFT JOIN safety_incidents i ON i.observation_id=s.id" + where,
            params,
        ).fetchone()
        observations = int(row["observations"])
        safety_incidents = int(row["safety_incidents"])
        return {
            "observations": observations,
            "safety_incidents": safety_incidents,
            "degraded_ratio": round(safety_incidents / observations, 6) if observations else 0.0,
            "average_pm25_ugm3": round(float(row["average_pm25_ugm3"]), 3),
            "average_co_ppm": round(float(row["average_co_ppm"]), 6),
            "average_supply_airflow": round(float(row["average_supply_airflow"]), 3),
        }

    def temple_breakdown(self, window: ReportWindow | None = None) -> list[dict[str, Any]]:
        window = window or ReportWindow()
        clauses, params = window.clauses("s.observed_at")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT n.code,n.name,n.temple_type,COUNT(s.id) AS observations,COUNT(i.id) AS safety_incidents,"
            "COALESCE(AVG(s.pm25_ugm3),0) AS average_pm25_ugm3,"
            "COALESCE(AVG(s.supply_airflow),0) AS average_supply_airflow "
            "FROM temple_sites n LEFT JOIN incense_observations s ON s.temple_id=n.id "
            "LEFT JOIN safety_incidents i ON i.observation_id=s.id" + where + " GROUP BY n.id ORDER BY safety_incidents DESC,n.code",
            params,
        ).fetchall()
        result = []
        for row in rows:
            observations = int(row["observations"])
            safety_incidents = int(row["safety_incidents"])
            result.append({
                "temple_code": row["code"],
                "temple_name": row["name"],
                "temple_type": row["temple_type"],
                "observations": observations,
                "safety_incidents": safety_incidents,
                "degraded_ratio": round(safety_incidents / observations, 6) if observations else 0.0,
                "average_pm25_ugm3": round(float(row["average_pm25_ugm3"]), 3),
                "average_supply_airflow": round(float(row["average_supply_airflow"]), 3),
            })
        return result

    def incense_profile_breakdown(self, window: ReportWindow | None = None) -> list[dict[str, Any]]:
        window = window or ReportWindow()
        clauses, params = window.clauses("s.observed_at")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT a.incense_code,a.name,a.activity_type,COUNT(s.id) AS observations,COUNT(i.id) AS safety_incidents,"
            "COALESCE(AVG(s.pm25_ugm3),0) AS average_pm25_ugm3,"
            "COALESCE(AVG(s.co_ppm),0) AS average_co_ppm "
            "FROM incense_profiles a LEFT JOIN incense_observations s ON s.incense_profile_id=a.id "
            "LEFT JOIN safety_incidents i ON i.observation_id=s.id" + where + " GROUP BY a.id ORDER BY safety_incidents DESC,a.incense_code",
            params,
        ).fetchall()
        result = []
        for row in rows:
            observations = int(row["observations"])
            safety_incidents = int(row["safety_incidents"])
            result.append({
                "incense_code": row["incense_code"],
                "app_name": row["name"],
                "activity_type": row["activity_type"],
                "observations": observations,
                "safety_incidents": safety_incidents,
                "degraded_ratio": round(safety_incidents / observations, 6) if observations else 0.0,
                "average_pm25_ugm3": round(float(row["average_pm25_ugm3"]), 3),
                "average_co_ppm": round(float(row["average_co_ppm"]), 6),
            })
        return result

    def hall_breakdown(self, temple_code: str | None = None, window: ReportWindow | None = None) -> list[dict[str, Any]]:
        window = window or ReportWindow()
        clauses, params = window.clauses("x.observed_at")
        if temple_code:
            clauses.append("n.code=?")
            params.append(temple_code)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT n.code AS temple_code,n.name AS temple_name,g.code AS hall_code,g.name AS hall_name,"
            "COUNT(x.id) AS observations,COUNT(i.id) AS safety_incidents,"
            "COALESCE(AVG(x.pm25_ugm3),0) AS average_pm25_ugm3,"
            "COALESCE(AVG(x.co_ppm),0) AS average_co_ppm,"
            "COALESCE(AVG(x.supply_airflow),0) AS average_supply_airflow "
            "FROM worship_halls g JOIN temple_sites n ON n.id=g.temple_id "
            "LEFT JOIN incense_observations x ON x.hall_id=g.id "
            "LEFT JOIN safety_incidents i ON i.observation_id=x.id" + where +
            " GROUP BY g.id ORDER BY safety_incidents DESC,n.code,g.visit_order,g.id",
            params,
        ).fetchall()
        result = []
        for row in rows:
            observations = int(row["observations"])
            safety_incidents = int(row["safety_incidents"])
            result.append({
                "temple_code": row["temple_code"],
                "temple_name": row["temple_name"],
                "hall_code": row["hall_code"],
                "hall_name": row["hall_name"],
                "observations": observations,
                "safety_incidents": safety_incidents,
                "degraded_ratio": round(safety_incidents / observations, 6) if observations else 0.0,
                "average_pm25_ugm3": round(float(row["average_pm25_ugm3"]), 3),
                "average_co_ppm": round(float(row["average_co_ppm"]), 6),
                "average_supply_airflow": round(float(row["average_supply_airflow"]), 3),
            })
        return result

    def severity_distribution(self, temple_code: str | None = None, window: ReportWindow | None = None) -> dict[str, Any]:
        window = window or ReportWindow()
        clauses, params = window.clauses("i.opened_at")
        if temple_code:
            clauses.append("n.code=?")
            params.append(temple_code)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT i.severity,i.state,COUNT(*) AS amount FROM safety_incidents i "
            "JOIN temple_sites n ON n.id=i.temple_id" + where +
            " GROUP BY i.severity,i.state ORDER BY i.severity,i.state",
            params,
        ).fetchall()
        severities: dict[str, int] = {}
        states: dict[str, int] = {}
        matrix: list[dict[str, Any]] = []
        for row in rows:
            amount = int(row["amount"])
            severities[row["severity"]] = severities.get(row["severity"], 0) + amount
            states[row["state"]] = states.get(row["state"], 0) + amount
            matrix.append({"severity": row["severity"], "state": row["state"], "amount": amount})
        total = sum(severities.values())
        critical = severities.get("critical", 0)
        return {
            "total": total,
            "severities": severities,
            "states": states,
            "matrix": matrix,
            "critical_ratio": round(critical / total, 6) if total else 0.0,
        }

    def stale_open_safety_incidents(self, before: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if not before:
            raise ValidationError("必须提供截止时间")
        if not 1 <= limit <= 500:
            raise ValidationError("每页事件数量必须在 1 到 500 之间")
        rows = self.connection.execute(
            "SELECT i.id,i.severity,i.state,i.opened_at,n.code AS temple_code,g.code AS hall_code,"
            "a.incense_code,x.steward_hash FROM safety_incidents i "
            "JOIN temple_sites n ON n.id=i.temple_id "
            "LEFT JOIN worship_halls g ON g.id=i.hall_id "
            "JOIN incense_profiles a ON a.id=i.incense_profile_id "
            "JOIN incense_observations x ON x.id=i.observation_id "
            "WHERE i.state IN ('open','mitigating') AND i.opened_at<? "
            "ORDER BY CASE i.severity WHEN 'critical' THEN 3 WHEN 'major' THEN 2 ELSE 1 END DESC,i.opened_at,i.id LIMIT ?",
            (before, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def ventilation_snapshot(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT n.code AS temple_code,n.name AS temple_name,g.code AS hall_code,g.name AS hall_name,"
            "COALESCE(g.ventilation_capacity,n.ventilation_capacity) AS ventilation_capacity,"
            "COALESCE(SUM(CASE WHEN r.state='held' THEN r.supply_airflow ELSE 0 END),0) AS held_supply_airflow,"
            "COUNT(DISTINCT CASE WHEN r.state='held' THEN r.mitigation_session_id END) AS active_mitigation_sessions "
            "FROM temple_sites n LEFT JOIN worship_halls g ON g.temple_id=n.id "
            "LEFT JOIN ventilation_reservations r ON r.temple_id=n.id AND r.hall_id IS g.id "
            "GROUP BY n.id,g.id ORDER BY n.code,g.visit_order,g.id"
        ).fetchall()
        result = []
        for row in rows:
            ventilation = float(row["ventilation_capacity"])
            held = float(row["held_supply_airflow"])
            result.append({
                "temple_code": row["temple_code"],
                "temple_name": row["temple_name"],
                "hall_code": row["hall_code"],
                "hall_name": row["hall_name"],
                "ventilation_capacity": ventilation,
                "held_supply_airflow": round(held, 3),
                "available_supply_airflow": round(max(0.0, ventilation - held), 3),
                "utilization": round(held / ventilation, 6) if ventilation else 0.0,
                "active_mitigation_sessions": int(row["active_mitigation_sessions"]),
            })
        return result

    def mitigation_outcomes(self, window: ReportWindow | None = None) -> dict[str, Any]:
        window = window or ReportWindow()
        clauses, params = window.clauses("started_at")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT status,COUNT(*) AS amount,COALESCE(AVG(allocated_supply_airflow),0) AS average_supply_airflow "
            "FROM mitigation_sessions" + where + " GROUP BY status",
            params,
        ).fetchall()
        states = {row["status"]: int(row["amount"]) for row in rows}
        total = sum(states.values())
        completed = states.get("completed", 0)
        return {
            "states": states,
            "total": total,
            "completion_ratio": round(completed / total, 6) if total else 0.0,
            "average_allocated_supply_airflow": round(
                sum(float(row["average_supply_airflow"]) * int(row["amount"]) for row in rows) / total,
                3,
            ) if total else 0.0,
        }

    def event_timeline(self, *, after_id: int = 0, limit: int = 100) -> dict[str, Any]:
        if after_id < 0:
            raise ValidationError("游标不能小于零")
        if not 1 <= limit <= 500:
            raise ValidationError("每页事件数量必须在 1 到 500 之间")
        rows = self.connection.execute(
            "SELECT e.id,e.mitigation_session_id,e.event_type,e.actor,e.detail_json,e.created_at,s.temple_id,n.code AS temple_code "
            "FROM mitigation_events e JOIN mitigation_sessions s ON s.id=e.mitigation_session_id "
            "JOIN temple_sites n ON n.id=s.temple_id WHERE e.id>? ORDER BY e.id LIMIT ?",
            (after_id, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        items = []
        for row in selected:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            items.append(item)
        next_cursor = items[-1]["id"] if items else after_id
        return {"items": items, "next_cursor": next_cursor, "has_more": has_more}
