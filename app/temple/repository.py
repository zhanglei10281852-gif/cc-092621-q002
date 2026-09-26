from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def rows_dict(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


class TempleRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def temple_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM temple_sites WHERE code=?", (code,)).fetchone()

    def temple_by_id(self, temple_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM temple_sites WHERE id=?", (temple_id,)).fetchone()

    def list_temples(self, *, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM temple_sites"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY name,id"
        return rows_dict(self.connection.execute(sql, params).fetchall())

    def hall_by_code(self, temple_id: int, code: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM worship_halls WHERE temple_id=? AND code=?",
            (temple_id, code),
        ).fetchone()

    def hall_by_id(self, hall_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM worship_halls WHERE id=?", (hall_id,)).fetchone()

    def halls(self, temple_id: int) -> list[dict[str, Any]]:
        return rows_dict(
            self.connection.execute(
                "SELECT * FROM worship_halls WHERE temple_id=? ORDER BY visit_order,id",
                (temple_id,),
            ).fetchall()
        )

    def incense_profile_by_code(self, incense_code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM incense_profiles WHERE incense_code=?", (incense_code,)).fetchone()

    def incense_profile_by_id(self, incense_profile_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM incense_profiles WHERE id=?", (incense_profile_id,)).fetchone()

    def list_incense_profiles(self, *, activity_type: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM incense_profiles"
        params: list[Any] = []
        if activity_type:
            sql += " WHERE activity_type=?"
            params.append(activity_type)
        sql += " ORDER BY activity_type,name,id"
        return rows_dict(self.connection.execute(sql, params).fetchall())

    def next_safety_policy_version(self, temple_id: int) -> int:
        return int(
            self.connection.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 FROM safety_policy_versions WHERE temple_id=?",
                (temple_id,),
            ).fetchone()[0]
        )

    def safety_policy_by_id(self, safety_policy_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM safety_policy_versions WHERE id=?", (safety_policy_id,)).fetchone()

    def safety_policy_by_digest(self, temple_id: int, digest: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM safety_policy_versions WHERE temple_id=? AND rules_digest=?",
            (temple_id, digest),
        ).fetchone()

    def effective_safety_policy(self, temple_id: int, now: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM safety_policy_versions WHERE temple_id=? AND state='published' AND effective_from<=? "
            "ORDER BY effective_from DESC,version_no DESC LIMIT 1",
            (temple_id, now),
        ).fetchone()

    def policies(self, temple_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM safety_policy_versions WHERE temple_id=? ORDER BY version_no DESC",
            (temple_id,),
        ).fetchall()
        return [self._safety_policy(row) for row in rows]

    def observation_by_key(self, observation_key: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM incense_observations WHERE observation_key=?", (observation_key,)).fetchone()

    def observation_by_id(self, observation_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM incense_observations WHERE id=?", (observation_id,)).fetchone()

    def safety_incident_by_observation(self, observation_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM safety_incidents WHERE observation_id=?", (observation_id,)).fetchone()

    def safety_incident_by_id(self, safety_incident_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM safety_incidents WHERE id=?", (safety_incident_id,)).fetchone()

    def open_safety_incidents(self, temple_id: int | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM safety_incidents WHERE state IN ('open','mitigating')"
        params: list[Any] = []
        if temple_id is not None:
            sql += " AND temple_id=?"
            params.append(temple_id)
        sql += " ORDER BY CASE severity WHEN 'critical' THEN 3 WHEN 'major' THEN 2 ELSE 1 END DESC,opened_at,id LIMIT ?"
        params.append(limit)
        return [self._safety_incident(row) for row in self.connection.execute(sql, params).fetchall()]

    def mitigation_session_by_safety_incident(self, safety_incident_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM mitigation_sessions WHERE safety_incident_id=?", (safety_incident_id,)).fetchone()

    def mitigation_session_by_id(self, mitigation_session_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM mitigation_sessions WHERE id=?", (mitigation_session_id,)).fetchone()

    def mitigation_events(self, mitigation_session_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM mitigation_events WHERE mitigation_session_id=? ORDER BY id", (mitigation_session_id,)).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            result.append(item)
        return result

    def active_ventilation(self, temple_id: int, hall_id: int | None) -> dict[str, float]:
        row = self.connection.execute(
            "SELECT COALESCE(SUM(supply_airflow),0),COALESCE(SUM(exhaust_airflow),0),COUNT(*) "
            "FROM ventilation_reservations WHERE temple_id=? AND hall_id IS ? AND state='held'",
            (temple_id, hall_id),
        ).fetchone()
        return {"supply_airflow": float(row[0]), "exhaust_airflow": float(row[1]), "mitigation_sessions": int(row[2])}

    def active_authorization(self, steward_hash: str, temple_id: int, now: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM steward_authorizations WHERE steward_hash=? AND temple_id=? AND state='active' "
            "AND valid_from<=? AND valid_until>? ORDER BY valid_until DESC,id DESC LIMIT 1",
            (steward_hash, temple_id, now, now),
        ).fetchone()

    def mitigation_session_detail(self, mitigation_session_id: int) -> dict[str, Any] | None:
        row = self.mitigation_session_by_id(mitigation_session_id)
        if row is None:
            return None
        result = dict(row)
        result["events"] = self.mitigation_events(mitigation_session_id)
        reservation = self.connection.execute(
            "SELECT * FROM ventilation_reservations WHERE mitigation_session_id=?",
            (mitigation_session_id,),
        ).fetchone()
        result["reservation"] = row_dict(reservation)
        return result

    def summary(self) -> dict[str, Any]:
        temples = self.connection.execute("SELECT status,COUNT(*) FROM temple_sites GROUP BY status").fetchall()
        safety_incidents = self.connection.execute("SELECT state,COUNT(*) FROM safety_incidents GROUP BY state").fetchall()
        mitigation_sessions = self.connection.execute("SELECT status,COUNT(*) FROM mitigation_sessions GROUP BY status").fetchall()
        observations = int(self.connection.execute("SELECT COUNT(*) FROM incense_observations").fetchone()[0])
        return {
            "temples": {row[0]: row[1] for row in temples},
            "safety_incidents": {row[0]: row[1] for row in safety_incidents},
            "mitigation_sessions": {row[0]: row[1] for row in mitigation_sessions},
            "observations": observations,
        }

    @staticmethod
    def _safety_policy(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["rules"] = json.loads(result.pop("rules_json"))
        return result

    @staticmethod
    def _safety_incident(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["reasons"] = json.loads(result.pop("reasons_json"))
        return result
