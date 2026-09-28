from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import request_fingerprint
from app.database import get_connection, transaction
from app.temple.authorization_ingest import (
    canonical_content,
    content_digest,
    describe_differences,
    stored_content,
)
from app.temple.repository import TempleRepository
from app.temple.rules import DEFAULT_RULES, allocation_for, canonical_rules, judge_quality
from app.temple.schema import ensure_temple_schema


@dataclass(frozen=True, slots=True)
class AuthorizationResult:
    body: dict[str, Any]
    outcome: str  # created | replayed


class TempleSafetyService:
    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_temple_schema(self.connection)
        self.clock = clock or SystemClock()
        self.repository = TempleRepository(self.connection)

    def create_temple(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO temple_sites(code,name,temple_type,timezone,max_concurrent_mitigation_sessions,ventilation_capacity,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (payload["code"], payload["name"], payload["temple_type"], payload["timezone"], payload["max_concurrent_mitigation_sessions"], payload["ventilation_capacity"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("寺院编码已存在") from exc
            return dict(TempleRepository(connection).temple_by_id(cursor.lastrowid))

    def list_temples(self, status: str | None = None) -> list[dict[str, Any]]:
        return self.repository.list_temples(status=status)

    def add_hall(self, temple_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple(temple_code)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO worship_halls(temple_id,code,name,visit_order,expected_visit_seconds,ventilation_capacity,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (temple["id"], payload["code"], payload["name"], payload["visit_order"], payload["expected_visit_seconds"], payload["ventilation_capacity"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("殿堂编码或顺序已存在") from exc
            return dict(TempleRepository(connection).hall_by_id(cursor.lastrowid))

    def create_incense_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO incense_profiles(incense_code,name,activity_type,pm25_target,co_target,min_supply_airflow,min_exhaust_airflow,default_risk_priority,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (payload["incense_code"], payload["name"], payload["activity_type"], payload["pm25_target"], payload["co_target"], payload["min_supply_airflow"], payload["min_exhaust_airflow"], payload["default_risk_priority"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("香火活动编码已存在") from exc
            return dict(TempleRepository(connection).incense_profile_by_id(cursor.lastrowid))

    def list_incense_profiles(self, activity_type: str | None = None) -> list[dict[str, Any]]:
        return self.repository.list_incense_profiles(activity_type=activity_type)

    def create_safety_policy(self, temple_code: str, rules: dict[str, Any], actor: str) -> dict[str, Any]:
        temple = self._temple(temple_code)
        text, digest = canonical_rules(rules)
        existing = self.repository.safety_policy_by_digest(temple["id"], digest)
        if existing is not None:
            return TempleRepository._safety_policy(existing)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = TempleRepository(connection)
            version = repository.next_safety_policy_version(temple["id"])
            cursor = connection.execute(
                "INSERT INTO safety_policy_versions(temple_id,version_no,rules_json,rules_digest,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (temple["id"], version, text, digest, actor, now, now),
            )
            return TempleRepository._safety_policy(repository.safety_policy_by_id(cursor.lastrowid))

    def publish_safety_policy(self, safety_policy_id: int, actor: str, effective_from: str) -> dict[str, Any]:
        safety_policy = self.repository.safety_policy_by_id(safety_policy_id)
        if safety_policy is None:
            raise NotFoundError("策略版本不存在")
        if safety_policy["state"] == "retired":
            raise ConflictError("已退役策略不能发布")
        try:
            effective = to_storage(from_storage(effective_from))
        except ValueError as exc:
            raise ValidationError("生效时间格式不正确") from exc
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE safety_policy_versions SET state='retired',retired_at=?,updated_at=? WHERE temple_id=? AND state='published' AND id<>?",
                (now, now, safety_policy["temple_id"], safety_policy_id),
            )
            connection.execute(
                "UPDATE safety_policy_versions SET state='published',published_by=?,effective_from=?,retired_at=NULL,updated_at=? WHERE id=?",
                (actor, effective, now, safety_policy_id),
            )
            return TempleRepository._safety_policy(TempleRepository(connection).safety_policy_by_id(safety_policy_id))

    def add_authorization(self, payload: dict[str, Any], *, actor: str = "upstream") -> AuthorizationResult:
        """登记来源审批。

        来源审批号是不可变业务身份：
        - 首次到达：插入一条授权（created）；
        - 同审批号、同内容：返回原记录（replayed），不改变授权状态；
        - 同审批号、内容有任何差异：拒绝并记录内容冲突审计（content_conflict）。
        首次写入与冲突审计在同一个 IMMEDIATE 事务内完成，并发申请只会留下一个版本。
        """
        temple = self._temple(payload["temple_code"])
        try:
            start = to_storage(from_storage(payload["valid_from"]))
            end = to_storage(from_storage(payload["valid_until"]))
        except ValueError as exc:
            raise ValidationError("权益有效期格式不正确") from exc
        if end <= start:
            raise ValidationError("权益结束时间必须晚于开始时间")
        incoming = canonical_content(
            steward_hash=payload["steward_hash"],
            temple_id=temple["id"],
            authorization_code=payload["authorization_code"],
            valid_from=start,
            valid_until=end,
        )
        digest = content_digest(incoming)
        approval_id = payload["source_approval_id"]
        now = to_storage(self.clock.now())

        with transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM steward_authorizations WHERE source_approval_id=?",
                (approval_id,),
            ).fetchone()

            if existing is None:
                try:
                    cursor = connection.execute(
                        "INSERT INTO steward_authorizations(steward_hash,temple_id,authorization_code,valid_from,valid_until,source_approval_id,content_digest,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (incoming["steward_hash"], incoming["temple_id"], incoming["authorization_code"], incoming["valid_from"], incoming["valid_until"], approval_id, digest, now, now),
                    )
                except sqlite3.IntegrityError as exc:
                    # 并发竞争：对端已抢先提交，改为按既存记录判定为重放或冲突
                    existing = connection.execute(
                        "SELECT * FROM steward_authorizations WHERE source_approval_id=?",
                        (approval_id,),
                    ).fetchone()
                    if existing is None:
                        raise ConflictError("授权登记并发冲突，请按来源审批号重试查询") from exc
                else:
                    self._authorization_event(
                        connection,
                        source_approval_id=approval_id,
                        authorization_id=cursor.lastrowid,
                        event_type="created",
                        state="active",
                        content_digest=digest,
                        actor=actor,
                        now=now,
                    )
                    row = connection.execute(
                        "SELECT * FROM steward_authorizations WHERE id=?",
                        (cursor.lastrowid,),
                    ).fetchone()
                    return AuthorizationResult(dict(row), "created")

            result, conflict = self._resolve_existing_authorization(
                connection,
                existing=existing,
                incoming=incoming,
                approval_id=approval_id,
                incoming_temple_code=payload["temple_code"],
                digest=digest,
                actor=actor,
                now=now,
            )
            if result is not None:
                return result

        # 冲突审计已随事务提交，再向调用方返回拒绝
        raise conflict

    def _resolve_existing_authorization(
        self,
        connection: sqlite3.Connection,
        *,
        existing: sqlite3.Row,
        incoming: dict[str, Any],
        approval_id: str,
        incoming_temple_code: str,
        digest: str,
        actor: str,
        now: str,
    ) -> tuple[AuthorizationResult | None, ConflictError | None]:
        """对既存授权判定幂等重放或内容冲突；冲突时审计已落库并返回待抛异常。"""
        # 兼容历史行：content_digest 可能尚未回填，仅在确属同一内容时补齐指纹
        existing_digest = existing["content_digest"] or content_digest(stored_content(existing))

        if existing_digest == digest:
            if existing["content_digest"] != existing_digest:
                connection.execute(
                    "UPDATE steward_authorizations SET content_digest=? WHERE id=?",
                    (existing_digest, existing["id"]),
                )
            # 重放绝不改变状态：授权已暂停/取消/到期时留下专门的阻断审计事件
            event_type = "replayed" if existing["state"] == "active" else "state_replay_blocked"
            self._authorization_event(
                connection,
                source_approval_id=approval_id,
                authorization_id=existing["id"],
                event_type=event_type,
                state=existing["state"],
                content_digest=existing_digest,
                actor=actor,
                now=now,
            )
            result = dict(existing)
            result["content_digest"] = existing_digest
            return AuthorizationResult(result, "replayed"), None

        differences = describe_differences(
            existing,
            existing_temple_code=self._temple_code(connection, existing["temple_id"]),
            incoming_steward_hash=incoming["steward_hash"],
            incoming_temple_code=incoming_temple_code,
            incoming_authorization_code=incoming["authorization_code"],
            incoming_valid_from=incoming["valid_from"],
            incoming_valid_until=incoming["valid_until"],
        )
        self._authorization_event(
            connection,
            source_approval_id=approval_id,
            authorization_id=existing["id"],
            event_type="content_conflict",
            state=existing["state"],
            content_digest=existing_digest,
            differences=differences,
            actor=actor,
            now=now,
        )
        return None, ConflictError(
            "来源审批号已登记为不同的授权内容，拒绝重复登记",
            context={
                "source_approval_id": approval_id,
                "existing_state": existing["state"],
                "differences": differences,
            },
        )

    def list_authorization_events(
        self,
        source_approval_id: str | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM authorization_audit_events"
        conditions: list[str] = []
        params: list[Any] = []
        if source_approval_id:
            conditions.append("source_approval_id=?")
            params.append(source_approval_id)
        if event_type:
            conditions.append("event_type=?")
            params.append(event_type)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        events = []
        for row in self.connection.execute(sql, params).fetchall():
            item = dict(row)
            item["differences"] = json.loads(item.pop("differences_json"))
            events.append(item)
        return events

    @staticmethod
    def _temple_code(connection: sqlite3.Connection, temple_id: int) -> str:
        row = connection.execute("SELECT code FROM temple_sites WHERE id=?", (temple_id,)).fetchone()
        return row["code"] if row is not None else f"temple-id-{temple_id}"

    @staticmethod
    def _authorization_event(
        connection: sqlite3.Connection,
        *,
        source_approval_id: str,
        authorization_id: int | None,
        event_type: str,
        state: str,
        content_digest: str,
        actor: str,
        now: str,
        differences: list[dict[str, str]] | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO authorization_audit_events(source_approval_id,authorization_id,event_type,state,content_digest,differences_json,actor,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                source_approval_id,
                authorization_id,
                event_type,
                state,
                content_digest,
                json.dumps(differences or [], ensure_ascii=False, sort_keys=True),
                actor,
                now,
            ),
        )

    def ingest_observation(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple(payload["temple_code"])
        app = self._incense_profile(payload["incense_code"])
        hall = None
        if payload.get("hall_code"):
            hall = self.repository.hall_by_code(temple["id"], payload["hall_code"])
            if hall is None:
                raise NotFoundError("寺院殿堂不存在")
        try:
            observed = to_storage(from_storage(payload["observed_at"]))
        except ValueError as exc:
            raise ValidationError("观测时间格式不正确") from exc
        digest = request_fingerprint(payload)
        existing = self.repository.observation_by_key(payload["observation_key"])
        if existing is not None:
            if existing["payload_digest"] != digest:
                raise ConflictError("相同 observation_key 对应了不同观测内容")
            return self._observation_result(existing["id"])
        now = to_storage(self.clock.now())
        safety_policy = self.repository.effective_safety_policy(temple["id"], now)
        rules = json.loads(safety_policy["rules_json"]) if safety_policy else DEFAULT_RULES
        decision = judge_quality(payload, dict(app), rules)
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "INSERT INTO incense_observations(observation_key,temple_id,hall_id,incense_profile_id,steward_hash,sensor_class,visitor_density,pm25_ugm3,co_ppm,supply_airflow,exhaust_airflow,observed_at,received_at,payload_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (payload["observation_key"], temple["id"], hall["id"] if hall else None, app["id"], payload["steward_hash"], payload["sensor_class"], payload["visitor_density"], payload["pm25_ugm3"], payload["co_ppm"], payload["supply_airflow"], payload["exhaust_airflow"], observed, now, digest),
            )
            safety_incident_id = None
            if decision.degraded:
                safety_incident = connection.execute(
                    "INSERT INTO safety_incidents(observation_id,temple_id,hall_id,incense_profile_id,severity,reasons_json,opened_at) VALUES(?,?,?,?,?,?,?)",
                    (cursor.lastrowid, temple["id"], hall["id"] if hall else None, app["id"], decision.severity, json.dumps(decision.as_dict(), ensure_ascii=False, sort_keys=True), now),
                )
                safety_incident_id = safety_incident.lastrowid
            return {"observation_id": cursor.lastrowid, "safety_incident_id": safety_incident_id, "quality": decision.as_dict()}

    def ingest_batch(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        results = []
        for item in items:
            results.append(self.ingest_observation(item))
        return {"items": results, "accepted": len(results)}

    def start_mitigation(self, safety_incident_id: int, actor: str) -> dict[str, Any]:
        safety_incident = self.repository.safety_incident_by_id(safety_incident_id)
        if safety_incident is None:
            raise NotFoundError("安全隐患事件不存在")
        existing = self.repository.mitigation_session_by_safety_incident(safety_incident_id)
        if existing is not None:
            return self.repository.mitigation_session_detail(existing["id"])
        if safety_incident["state"] != "open":
            raise ConflictError("只有待处理事件可以启动缓解")
        observation = self.repository.observation_by_id(safety_incident["observation_id"])
        app = self.repository.incense_profile_by_id(safety_incident["incense_profile_id"])
        now_value = self.clock.now()
        now = to_storage(now_value)
        authorization = self.repository.active_authorization(observation["steward_hash"], safety_incident["temple_id"], now)
        if authorization is None:
            raise ConflictError("用户没有当前寺院的有效缓解权益")
        safety_policy = self.repository.effective_safety_policy(safety_incident["temple_id"], now)
        if safety_policy is None:
            raise ConflictError("寺院没有已生效的缓解策略")
        rules = json.loads(safety_policy["rules_json"])
        allocation = allocation_for(dict(app), safety_incident["severity"], rules)
        temple = self.repository.temple_by_id(safety_incident["temple_id"])
        hall = self.repository.hall_by_id(safety_incident["hall_id"]) if safety_incident["hall_id"] else None
        from app.temple.operations import TempleRestorationService
        closure = TempleRestorationService(self.connection, self.clock).blocks_new_mitigation_session(safety_incident["temple_id"], safety_incident["hall_id"], now)
        if closure is not None:
            raise ConflictError("当前寺院处于维护窗口，不能启动新的缓解处置会话", context={"closure_code": closure["code"]})
        limit = int(hall["ventilation_capacity"] if hall else temple["ventilation_capacity"])
        used = self.repository.active_ventilation(safety_incident["temple_id"], safety_incident["hall_id"])
        if used["mitigation_sessions"] >= int(temple["max_concurrent_mitigation_sessions"]):
            raise ConflictError("寺院并发缓解处置会话已达到上限")
        if used["supply_airflow"] + allocation.supply_airflow > limit:
            raise ConflictError("殿堂送风容量不足")
        expires = to_storage(now_value + timedelta(seconds=allocation.duration_seconds))
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "INSERT INTO mitigation_sessions(safety_incident_id,steward_hash,incense_profile_id,temple_id,hall_id,safety_policy_version_id,allocated_supply_airflow,allocated_exhaust_airflow,priority,started_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (safety_incident_id, observation["steward_hash"], safety_incident["incense_profile_id"], safety_incident["temple_id"], safety_incident["hall_id"], safety_policy["id"], allocation.supply_airflow, allocation.exhaust_airflow, allocation.priority, now, expires),
            )
            connection.execute(
                "INSERT INTO ventilation_reservations(mitigation_session_id,temple_id,hall_id,supply_airflow,exhaust_airflow,held_at) VALUES(?,?,?,?,?,?)",
                (cursor.lastrowid, safety_incident["temple_id"], safety_incident["hall_id"], allocation.supply_airflow, allocation.exhaust_airflow, now),
            )
            connection.execute("UPDATE safety_incidents SET state='mitigating',version=version+1 WHERE id=?", (safety_incident_id,))
            self._event(connection, cursor.lastrowid, "started", actor, {"safety_policy_version": safety_policy["version_no"]}, now)
            return TempleRepository(connection).mitigation_session_detail(cursor.lastrowid)

    def finish_mitigation_session(self, mitigation_session_id: int, actor: str, reason: str, result: str) -> dict[str, Any]:
        mitigation_session = self.repository.mitigation_session_by_id(mitigation_session_id)
        if mitigation_session is None:
            raise NotFoundError("缓解处置会话不存在")
        if mitigation_session["status"] != "active":
            return self.repository.mitigation_session_detail(mitigation_session_id)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE mitigation_sessions SET status=?,ended_at=?,end_reason=?,version=version+1 WHERE id=? AND status='active'",
                (result, now, reason, mitigation_session_id),
            )
            connection.execute("UPDATE ventilation_reservations SET state='released',released_at=? WHERE mitigation_session_id=? AND state='held'", (now, mitigation_session_id))
            safety_incident_state = "resolved" if result == "completed" else "open"
            connection.execute("UPDATE safety_incidents SET state=?,resolved_at=?,version=version+1 WHERE id=?", (safety_incident_state, now if result == "completed" else None, mitigation_session["safety_incident_id"]))
            self._event(connection, mitigation_session_id, result, actor, {"reason": reason}, now)
            return TempleRepository(connection).mitigation_session_detail(mitigation_session_id)

    def expire_mitigation_sessions(self, actor: str = "mitigation_session-reaper") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        rows = self.connection.execute("SELECT id FROM mitigation_sessions WHERE status='active' AND expires_at<=? ORDER BY id", (now,)).fetchall()
        expired = []
        for row in rows:
            with transaction(immediate=True) as connection:
                mitigation_session = TempleRepository(connection).mitigation_session_by_id(row["id"])
                if mitigation_session is None or mitigation_session["status"] != "active":
                    continue
                connection.execute("UPDATE mitigation_sessions SET status='expired',ended_at=?,end_reason='duration_elapsed',version=version+1 WHERE id=?", (now, row["id"]))
                connection.execute("UPDATE ventilation_reservations SET state='released',released_at=? WHERE mitigation_session_id=? AND state='held'", (now, row["id"]))
                connection.execute("UPDATE safety_incidents SET state='open',version=version+1 WHERE id=?", (mitigation_session["safety_incident_id"],))
                self._event(connection, row["id"], "expired", actor, {}, now)
                expired.append(row["id"])
        return {"expired": expired}

    def open_safety_incidents(self, temple_code: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        temple_id = self._temple(temple_code)["id"] if temple_code else None
        return self.repository.open_safety_incidents(temple_id, limit=limit)

    def get_mitigation_session(self, mitigation_session_id: int) -> dict[str, Any]:
        result = self.repository.mitigation_session_detail(mitigation_session_id)
        if result is None:
            raise NotFoundError("缓解处置会话不存在")
        return result

    def summary(self) -> dict[str, Any]:
        return self.repository.summary()

    def seed_demo(self) -> dict[str, Any]:
        temple = self.repository.temple_by_code("lingyun-temple")
        if temple is None:
            temple = self.create_temple({"code": "lingyun-temple", "name": "凌云古寺", "temple_type": "heritage", "timezone": "Asia/Shanghai", "max_concurrent_mitigation_sessions": 5000, "ventilation_capacity": 3000})
            self.add_hall("lingyun-temple", {"code": "main-hall", "name": "大雄宝殿", "visit_order": 1, "expected_visit_seconds": 900, "ventilation_capacity": 1200})
        app = self.repository.incense_profile_by_code("festival-incense")
        if app is None:
            app = self.create_incense_profile({"incense_code": "festival-incense", "name": "节庆香火", "activity_type": "festival", "pm25_target": 100, "co_target": 0.01, "min_supply_airflow": 8, "min_exhaust_airflow": 4, "default_risk_priority": 70})
        safety_policy = self.create_safety_policy("lingyun-temple", DEFAULT_RULES, "demo")
        if safety_policy["state"] != "published":
            safety_policy = self.publish_safety_policy(safety_policy["id"], "demo", to_storage(self.clock.now()))
        return {"temple": temple, "incense_profile": app, "safety_policy": safety_policy}

    def _observation_result(self, observation_id: int) -> dict[str, Any]:
        observation = self.repository.observation_by_id(observation_id)
        safety_incident = self.repository.safety_incident_by_observation(observation_id)
        return {"observation_id": observation_id, "safety_incident_id": safety_incident["id"] if safety_incident else None, "duplicate": True, "observation": dict(observation)}

    def _temple(self, code: str) -> sqlite3.Row:
        row = self.repository.temple_by_code(code)
        if row is None:
            raise NotFoundError("寺院不存在")
        return row

    def _incense_profile(self, code: str) -> sqlite3.Row:
        row = self.repository.incense_profile_by_code(code)
        if row is None:
            raise NotFoundError("香火活动画像不存在")
        return row

    @staticmethod
    def _event(connection: sqlite3.Connection, mitigation_session_id: int, event_type: str, actor: str, detail: dict[str, Any], now: str) -> None:
        connection.execute(
            "INSERT INTO mitigation_events(mitigation_session_id,event_type,actor,detail_json,created_at) VALUES(?,?,?,?,?)",
            (mitigation_session_id, event_type, actor, json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )
