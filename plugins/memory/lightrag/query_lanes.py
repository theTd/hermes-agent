from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QueryPlan:
    lane: str


IDENTITY_PATTERNS = ("我是谁", "用户是谁", "记得我什么", "我的偏好", "我的名字")
SHARED_PATTERNS = ("群里", "项目", "真和平", "极光想法", "大家怎么说")
MIXED_PATTERNS = ("我在这个项目", "结合我的", "群里大家怎么看我")


def classify_query(query: str) -> QueryPlan:
    text = str(query or "")
    if any(pattern in text for pattern in MIXED_PATTERNS):
        return QueryPlan("mixed")
    if any(pattern in text for pattern in IDENTITY_PATTERNS):
        return QueryPlan("identity")
    if any(pattern in text for pattern in SHARED_PATTERNS):
        return QueryPlan("shared_context")
    return QueryPlan("mixed")
