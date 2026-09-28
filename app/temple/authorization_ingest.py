"""来源审批号幂等与内容冲突判定的纯逻辑。

来源审批号（source_approval_id）是授权的不可变业务身份：
同一审批号携带完全一致的业务内容到达时视为上游重试，任何差异都视为内容冲突。
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.core.security import request_fingerprint


def canonical_content(
    *,
    steward_hash: str,
    temple_id: int,
    authorization_code: str,
    valid_from: str,
    valid_until: str,
) -> dict[str, Any]:
    """规范化后的授权内容；时间必须是已归一化到 UTC 的存储格式。"""
    return {
        "steward_hash": steward_hash,
        "temple_id": temple_id,
        "authorization_code": authorization_code,
        "valid_from": valid_from,
        "valid_until": valid_until,
    }


def stored_content(row: Any) -> dict[str, Any]:
    """从已存储的授权行提取规范内容（兼容 content_digest 尚未回填的历史行）。"""
    return canonical_content(
        steward_hash=row["steward_hash"],
        temple_id=row["temple_id"],
        authorization_code=row["authorization_code"],
        valid_from=row["valid_from"],
        valid_until=row["valid_until"],
    )


def content_digest(content: dict[str, Any]) -> str:
    return request_fingerprint(content)


def steward_token(steward_hash: str) -> str:
    """人员标识的脱敏指纹：足以区分/定位两名值守员，但不暴露完整 steward_hash。"""
    return "steward-" + hashlib.sha256(steward_hash.encode("utf-8")).hexdigest()[:10]


def describe_differences(
    existing_row: Any,
    *,
    existing_temple_code: str,
    incoming_steward_hash: str,
    incoming_temple_code: str,
    incoming_authorization_code: str,
    incoming_valid_from: str,
    incoming_valid_until: str,
) -> list[dict[str, str]]:
    """逐字段生成差异摘要；人员字段只出现脱敏指纹，时间与业务编码保留原值以便定位。"""
    candidates = (
        ("steward_hash", steward_token(existing_row["steward_hash"]), steward_token(incoming_steward_hash)),
        ("temple_code", existing_temple_code, incoming_temple_code),
        ("authorization_code", existing_row["authorization_code"], incoming_authorization_code),
        ("valid_from", existing_row["valid_from"], incoming_valid_from),
        ("valid_until", existing_row["valid_until"], incoming_valid_until),
    )
    return [
        {"field": field, "existing": old_value, "incoming": new_value}
        for field, old_value, new_value in candidates
        if old_value != new_value
    ]
