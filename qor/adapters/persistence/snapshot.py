"""Save and load one organization's in-process facts.

The file is a snapshot of records that already exist. Loading it does not run
a task, open a task, or call a model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from qor.adapters.persistence.llm_usage import LlmUsageRecord
from qor.adapters.persistence.run_record import RunRecord
from qor.domain.action import Action
from qor.domain.artifact import Artifact
from qor.domain.audit import AuditRecord, Event, StateTransition
from qor.domain.decision import Decision
from qor.domain.event_route import EventRoute
from qor.domain.lesson import Lesson
from qor.domain.notification import NotificationIntent
from qor.domain.organization import Agent, Capability, Department, Organization, Role
from qor.domain.outcome import Outcome
from qor.domain.responsibility import Responsibility
from qor.domain.review import Review
from qor.domain.schedule import Schedule
from qor.domain.task import Task, TaskAssignment, TaskNode, TaskPlan
from qor.services.runtime import Runtime

__all__ = ["load_snapshot", "save_snapshot"]

_VERSION = 1

_REPO_MODELS: dict[str, type[BaseModel]] = {
    "organizations": Organization,
    "departments": Department,
    "roles": Role,
    "capabilities": Capability,
    "agents": Agent,
    "tasks": Task,
    "plans": TaskPlan,
    "nodes": TaskNode,
    "assignments": TaskAssignment,
    "artifacts": Artifact,
    "reviews": Review,
    "decisions": Decision,
    "actions": Action,
    "outcomes": Outcome,
    "lessons": Lesson,
    "schedules": Schedule,
    "responsibilities": Responsibility,
}


def save_snapshot(runtime: Runtime, path: Path) -> None:
    payload = {
        "version": _VERSION,
        "organization_id": str(runtime.organization_id) if runtime.organization_id else None,
        "capability_index": {key: str(value) for key, value in runtime.capability_index.items()},
        "role_index": {key: str(value) for key, value in runtime.role_index.items()},
        "agent_index": {key: str(value) for key, value in runtime.agent_index.items()},
        "log": {
            "transitions": [_dump(item) for item in runtime.log.transitions()],
            "events": [_dump(item) for item in runtime.log.events()],
            "audit": [_dump(item) for item in runtime.log.audit_records()],
        },
        "repos": {
            name: [_dump(item) for item in getattr(runtime.repos, name).all()]
            for name in _REPO_MODELS
        },
        "event_routes": [_dump(item) for item in runtime.event_routes.all()],
        "notifications": [_dump(item) for item in runtime.notifications.all()],
        "llm_calls": [_dump(item) for item in runtime.llm_calls],
        "run": _dump(runtime.run) if runtime.run is not None else None,
        "run_history": [_dump(item) for item in runtime.run_history],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_snapshot(runtime: Runtime, path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != _VERSION:
        raise ValueError(f"unsupported organization snapshot version: {payload.get('version')}")
    log = payload["log"]
    runtime.log.restore(
        transitions=[StateTransition.model_validate(item) for item in log["transitions"]],
        events=[Event.model_validate(item) for item in log["events"]],
        audit=[AuditRecord.model_validate(item) for item in log["audit"]],
    )
    repos = payload["repos"]
    for name, model in _REPO_MODELS.items():
        store = getattr(runtime.repos, name)
        for item in repos[name]:
            store.insert(model.model_validate(item))
            runtime.refs.absorb(item["ref"])
    for item in payload["event_routes"]:
        runtime.event_routes.insert(EventRoute.model_validate(item))
        runtime.refs.absorb(item["ref"])
    for item in payload["notifications"]:
        runtime.notifications.save(NotificationIntent.model_validate(item))
    org_id = payload.get("organization_id")
    runtime.organization_id = UUID(org_id) if org_id else None
    runtime.capability_index = {
        key: UUID(value) for key, value in payload["capability_index"].items()
    }
    runtime.role_index = {key: UUID(value) for key, value in payload["role_index"].items()}
    runtime.agent_index = {key: UUID(value) for key, value in payload["agent_index"].items()}
    runtime.llm_calls = [LlmUsageRecord.model_validate(item) for item in payload["llm_calls"]]
    if payload.get("run"):
        runtime.run = RunRecord.model_validate(payload["run"])
    runtime.run_history = [
        RunRecord.model_validate(item) for item in payload.get("run_history", [])
    ]
    if not runtime.log.verify_audit_chain():
        raise ValueError("saved audit chain does not verify")


def _dump(item: Any) -> dict[str, Any]:
    return item.model_dump(mode="json")
