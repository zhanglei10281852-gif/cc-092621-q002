from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import transaction
from app.repositories.audit import AuditRepository
from app.temple.repository import TempleRepository

# 参与“同一来源审批单”内容比对的业务字段。时间统一归一化为 UTC，
# 因而同一时刻的不同时区写法会被视为同一次申请。
_IDENTITY_FIELDS = ("steward_hash", "temple_code", "authorization_code", "valid_from", "valid_until")

_FIELD_LABELS = {
    "temple_code": "寺院",
    "steward_hash": "值守员",
    "authorization_code": "授权类别",
    "valid_from": "有效期开始",
    "valid_until": "有效期结束",
}


def canonical_digest(identity: dict[str, Any]) -> str:
    compact = json.dumps(
        {field: identity[field] for field in _IDENTITY_FIELDS},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(compact.encode()).hexdigest()


def steward_hint(value: str) -> str:
    """只暴露值守员标识的首尾片段，足以串联同一人，不足以还原完整标识。"""
    if len(value) <= 6:
        return value[:1] + "***"
    return f"{value[:4]}***{value[-2:]}"


class AuthorizationService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = TempleRepository(connection)

    def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self.repository.temple_by_code(payload["temple_code"])
        if temple is None:
            raise NotFoundError("寺院不存在")
        try:
            valid_from = to_storage(from_storage(payload["valid_from"]))
            valid_until = to_storage(from_storage(payload["valid_until"]))
        except (TypeError, ValueError) as exc:
            raise ValidationError("权益有效期格式不正确") from exc
        if valid_until <= valid_from:
            raise ValidationError("权益结束时间必须晚于开始时间")
        identity = {
            "steward_hash": payload["steward_hash"],
            "temple_code": payload["temple_code"],
            "authorization_code": payload["authorization_code"],
            "valid_from": valid_from,
            "valid_until": valid_until,
        }
        digest = canonical_digest(identity)
        source_approval_id = payload["source_approval_id"]
        now = to_storage(self.clock.now())
        conflict: dict[str, Any] | None = None
        # 首次写入、幂等判定与冲突审计共用同一个 IMMEDIATE 事务：
        # SQLite 写锁会串行化并发申请，保证同一来源审批号最多只有一个可信版本。
        with transaction(immediate=True) as connection:
            repository = TempleRepository(connection)
            existing = repository.authorization_by_source(source_approval_id)
            if existing is None:
                cursor = connection.execute(
                    "INSERT INTO steward_authorizations(steward_hash,temple_id,authorization_code,valid_from,valid_until,"
                    "source_approval_id,content_digest,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        identity["steward_hash"],
                        temple["id"],
                        identity["authorization_code"],
                        valid_from,
                        valid_until,
                        source_approval_id,
                        digest,
                        now,
                        now,
                    ),
                )
                record = dict(repository.authorization_by_id(cursor.lastrowid))
                self._audit(
                    connection,
                    "authorization.created",
                    "success",
                    record,
                    metadata={"source_approval_id": source_approval_id, "content_digest": digest, "request_outcome": "created"},
                )
                return {"request_outcome": "created", **record}
            differences = self._differences(connection, dict(existing), identity)
            if not differences:
                # 完全一致的重放：原样返回既有记录，不写入也不改变生命周期状态。
                self._audit(
                    connection,
                    "authorization.replayed",
                    "success",
                    dict(existing),
                    metadata={
                        "source_approval_id": source_approval_id,
                        "content_digest": digest,
                        "request_outcome": "idempotent_retry",
                        "authorization_state": existing["state"],
                    },
                )
                return {"request_outcome": "idempotent_retry", **dict(existing)}
            # 同一来源审批号携带了不同内容：拒绝并在同一事务内留存冲突证据。
            connection.execute(
                "INSERT INTO authorization_conflicts(source_approval_id,authorization_id,incoming_digest,steward_hint,"
                "temple_code,authorization_code,valid_from,valid_until,differences_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    source_approval_id,
                    existing["id"],
                    digest,
                    steward_hint(identity["steward_hash"]),
                    identity["temple_code"],
                    identity["authorization_code"],
                    valid_from,
                    valid_until,
                    json.dumps(differences, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            self._audit(
                connection,
                "authorization.conflict",
                "failure",
                dict(existing),
                metadata={
                    "source_approval_id": source_approval_id,
                    "incoming_digest": digest,
                    "request_outcome": "content_conflict",
                    "differences": differences,
                },
            )
            conflict = {
                "source_approval_id": source_approval_id,
                "authorization_id": existing["id"],
                "differences": differences,
            }
        # 冲突记录已随事务提交，再向调用方返回拒绝。
        raise ConflictError("来源审批号已登记为不同授权内容，已拒绝并记录内容冲突", context=conflict)

    def detail(self, authorization_id: int) -> dict[str, Any]:
        record = self.repository.authorization_by_id(authorization_id)
        if record is None:
            raise NotFoundError("值守授权不存在")
        result = dict(record)
        temple = self.repository.temple_by_id(record["temple_id"])
        result["temple_code"] = temple["code"] if temple else None
        result["steward_hint"] = steward_hint(record["steward_hash"])
        result["lifecycle_events"] = self.repository.authorization_events(authorization_id)
        result["conflicts"] = self.repository.authorization_conflicts(authorization_id)
        return result

    def suspend_authorization(self, authorization_id: int, actor: str, reason: str) -> dict[str, Any]:
        return self._transition(authorization_id, "suspended", actor, reason)

    def cancel_authorization(self, authorization_id: int, actor: str, reason: str) -> dict[str, Any]:
        return self._transition(authorization_id, "cancelled", actor, reason)

    def expire_due_authorizations(self, actor: str = "authorization-reaper") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        expired: list[int] = []
        with transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT id FROM steward_authorizations WHERE state IN ('active','suspended') AND valid_until<=? ORDER BY id",
                (now,),
            ).fetchall()
            for row in rows:
                self._apply_transition(connection, row["id"], "expired", actor, "授权有效期已结束", now)
                expired.append(row["id"])
        return {"expired": expired}

    def _transition(self, authorization_id: int, target_state: str, actor: str, reason: str) -> dict[str, Any]:
        record = self.repository.authorization_by_id(authorization_id)
        if record is None:
            raise NotFoundError("值守授权不存在")
        if record["state"] == target_state:
            return self.detail(authorization_id)
        if record["state"] in {"cancelled", "expired"}:
            raise ConflictError(f"授权已{record['state']}，不能再次变更")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            self._apply_transition(connection, authorization_id, target_state, actor, reason, now)
        return self.detail(authorization_id)

    def _apply_transition(
        self,
        connection: sqlite3.Connection,
        authorization_id: int,
        target_state: str,
        actor: str,
        reason: str,
        now: str,
    ) -> None:
        connection.execute(
            "UPDATE steward_authorizations SET state=?,updated_at=? WHERE id=?",
            (target_state, now, authorization_id),
        )
        connection.execute(
            "INSERT INTO authorization_events(authorization_id,event_type,actor,reason,created_at) VALUES(?,?,?,?,?)",
            (authorization_id, target_state, actor, reason, now),
        )
        record = TempleRepository(connection).authorization_by_id(authorization_id)
        self._audit(
            connection,
            f"authorization.{target_state}",
            "success",
            dict(record) if record else {"id": authorization_id},
            metadata={"reason": reason, "request_outcome": "lifecycle_change"},
        )

    def _differences(
        self,
        connection: sqlite3.Connection,
        existing: dict[str, Any],
        incoming: dict[str, Any],
    ) -> list[dict[str, str]]:
        temple = TempleRepository(connection).temple_by_id(existing["temple_id"])
        existing_identity = {
            "temple_code": temple["code"] if temple else None,
            "steward_hash": existing["steward_hash"],
            "authorization_code": existing["authorization_code"],
            "valid_from": existing["valid_from"],
            "valid_until": existing["valid_until"],
        }
        result: list[dict[str, str]] = []
        for field in _IDENTITY_FIELDS:
            old_value = existing_identity[field]
            new_value = incoming[field]
            if old_value == new_value:
                continue
            if field == "steward_hash":
                old_value = steward_hint(existing_identity["steward_hash"])
                new_value = steward_hint(incoming["steward_hash"])
            result.append(
                {
                    "field": field,
                    "label": _FIELD_LABELS[field],
                    "existing": str(old_value),
                    "incoming": str(new_value),
                }
            )
        return result

    def _audit(
        self,
        connection: sqlite3.Connection,
        action: str,
        outcome: str,
        after: dict[str, Any],
        *,
        metadata: dict[str, Any],
    ) -> None:
        AuditRepository(connection).append(
            actor_user_id=None,
            actor_name="source-approval-ingest",
            action=action,
            resource_type="steward_authorization",
            resource_id=after.get("id") or after.get("source_approval_id"),
            outcome=outcome,
            before=None,
            after=self._redacted_view(after),
            metadata=metadata,
            correlation_id=None,
            created_at=to_storage(self.clock.now()),
        )

    @staticmethod
    def _redacted_view(record: dict[str, Any]) -> dict[str, Any]:
        if "steward_hash" not in record:
            return record
        view = dict(record)
        view.pop("steward_hash", None)
        view["steward_hint"] = steward_hint(record["steward_hash"])
        return view
