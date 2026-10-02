"""Bootstrap the organization from configuration.

Two things worth noting:

* Agents are registered through the state machine like everything else. There is no
  back door that writes ``status="AVAILABLE"`` directly, because the repository
  would reject it.
* The I26 check runs here, at load time. A misconfigured role that hands an AI
  agent ``capability.risk_class.modify`` fails startup rather than being discovered
  the day it matters.
"""

from __future__ import annotations

from qor.domain.enums import OperatingMode, RiskClass, RiskLevel
from qor.domain.organization import (
    Agent,
    AgentCapability,
    Capability,
    Department,
    Organization,
    Role,
)
from qor.kernel.actor import ActorKind, ActorRef
from qor.services.authorization import Authorizer
from qor.services.runtime import Runtime
from qor.services.transitions import TransitionEngine

__all__ = ["Bootstrapper", "bootstrap"]

SYSTEM = ActorRef.system("QOR-BOOTSTRAP")


class Bootstrapper:
    def __init__(self, runtime: Runtime, engine: TransitionEngine, authorizer: Authorizer) -> None:
        self._rt = runtime
        self._engine = engine
        self._authz = authorizer

    def run(self) -> Runtime:
        self._create_organization()
        self._create_capabilities()
        self._create_roles()
        self._create_departments()
        self._create_agents()

        self._authz.verify_permission_catalog()
        self._authz.verify_no_ai_holds_exclusive_permissions()

        self._register_agents()
        return self._rt

    # ---- steps ---------------------------------------------------------

    def _create_organization(self) -> None:
        spec = self._rt.config.organization["organization"]
        now = self._rt.clock.now()
        oid, ref = self._rt.mint("ORG")
        org = Organization(
            id=oid,
            ref=ref,
            status="ACTIVE",
            created_at=now,
            created_by=SYSTEM,
            updated_at=now,
            name=spec["name"],
            mission=spec["mission"],
            operating_mode=OperatingMode(spec.get("operating_mode", "NORMAL")),
            charter_version=str(spec.get("charter_version", "1.0")),
            hard_limits={k: float(v) for k, v in (spec.get("hard_limits") or {}).items()},
        )
        # Organization has no state machine of its own in V0.1; it is a singleton
        # configuration root, so it is inserted rather than transitioned.
        self._rt.repos.organizations.insert(org)
        self._rt.organization_id = org.id

    def _create_capabilities(self) -> None:
        now = self._rt.clock.now()
        for spec in self._rt.config.capabilities["capabilities"]:
            cid, ref = self._rt.mint("CAP")
            cap = Capability(
                id=cid,
                ref=ref,
                status="ACTIVE",
                created_at=now,
                created_by=SYSTEM,
                updated_at=now,
                key=spec["key"],
                name=spec["name"],
                description=spec["description"],
                risk_class=RiskClass.parse(spec.get("risk_class", "READ_ONLY")),
                required_permissions=frozenset(spec.get("required_permissions", ())),
                mandatory_review_types=tuple(spec.get("mandatory_review_types", ())),
                verifiable=bool(spec.get("verifiable", True)),
            )
            self._rt.repos.capabilities.insert(cap)
            self._rt.capability_index[cap.key] = cap.id

    def _create_roles(self) -> None:
        now = self._rt.clock.now()
        org_id = self._rt.organization().id
        for spec in self._rt.config.roles["roles"]:
            rid, ref = self._rt.mint("ROLE")
            approve = spec.get("can_approve_up_to")
            role = Role(
                id=rid,
                ref=ref,
                status="ACTIVE",
                created_at=now,
                created_by=SYSTEM,
                updated_at=now,
                organization_id=org_id,
                name=spec["name"],
                purpose=spec["purpose"],
                granted_permissions=frozenset(spec.get("granted_permissions", ())),
                required_capabilities=frozenset(spec.get("required_capabilities", ())),
                max_risk_level=RiskLevel.parse(spec.get("max_risk_level", "ROUTINE")),
                can_approve_up_to=RiskLevel.parse(approve) if approve else None,
                human_only=bool(spec.get("human_only", False)),
            )
            self._rt.repos.roles.insert(role)
            self._rt.role_index[spec["key"]] = role.id

    def _create_departments(self) -> None:
        now = self._rt.clock.now()
        org_id = self._rt.organization().id
        for spec in self._rt.config.organization.get("departments", ()):
            did, ref = self._rt.mint("DEPT")
            dept = Department(
                id=did,
                ref=ref,
                status="ACTIVE",
                created_at=now,
                created_by=SYSTEM,
                updated_at=now,
                organization_id=org_id,
                name=spec["name"],
                mandate=spec["mandate"],
                owns_capabilities=tuple(spec.get("owns_capabilities", ())),
            )
            self._rt.repos.departments.insert(dept)

    def _create_agents(self) -> None:
        now = self._rt.clock.now()
        org = self._rt.organization()
        for spec in self._rt.config.organization["agents"]:
            aid, ref = self._rt.mint("AGT")
            unknown = [
                c["capability_key"]
                for c in spec.get("capabilities", ())
                if c["capability_key"] not in self._rt.capability_index
            ]
            if unknown:
                raise ValueError(f"agent '{spec['name']}' claims unknown capabilities {unknown}")
            agent = Agent(
                id=aid,
                ref=ref,
                status="OFFLINE",
                created_at=now,
                created_by=SYSTEM,
                updated_at=now,
                organization_id=org.id,
                kind=ActorKind(spec["kind"]),
                name=spec["name"],
                role_ids=tuple(self._rt.role_index[r] for r in spec.get("roles", ())),
                capabilities=tuple(
                    AgentCapability(
                        capability_key=c["capability_key"],
                        proficiency=float(c.get("proficiency", 0.5)),
                    )
                    for c in spec.get("capabilities", ())
                ),
                max_concurrent_assignments=int(spec.get("max_concurrent_assignments", 1)),
                trust_score=float(spec.get("trust_score", 0.5)),
            )
            self._rt.repos.agents.insert(agent)
            self._rt.agent_index[spec["key"]] = agent.id

        human_key = self._rt.config.organization.get("human_cio")
        if human_key:
            updated = org.model_copy(update={"human_cio_agent_id": self._rt.agent_index[human_key]})
            self._rt.repos.organizations.save(updated)

    def _register_agents(self) -> None:
        """Bring agents OFFLINE -> AVAILABLE through the state machine.

        Humans are registered too: the Human CIO must be AVAILABLE to be selected
        as an approver, and modelling that as a real state avoids a special case.
        """
        for agent in self._rt.repos.agents.all():
            fresh = self._rt.repos.agents.get(agent.id)
            self._engine.record_creation(
                fresh,
                actor=SYSTEM,
                reason_code="AGENT_REGISTERED",
                reason=f"seeded from organization.yaml as {fresh.kind}",
            )
            result = self._engine.fire(
                fresh,
                "register",
                actor=SYSTEM,
                reason_code="AGENT_REGISTERED",
                reason=f"{fresh.name} brought online during bootstrap",
            )
            self._rt.repos.agents.save(result.entity)


def bootstrap(runtime: Runtime) -> tuple[Runtime, TransitionEngine, Authorizer]:
    authorizer = Authorizer(runtime)
    engine = TransitionEngine(runtime, authorizer)
    Bootstrapper(runtime, engine, authorizer).run()
    return runtime, engine, authorizer
