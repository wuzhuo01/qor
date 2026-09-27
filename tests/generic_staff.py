"""A test organization that supplies its own work capabilities.

The product default does not include these keys. They are configuration for
contract tests, not a QOR business catalog.
"""

from __future__ import annotations

import copy

from qor.services.config import QORConfig

ACCOUNT_RECORD = "account.record"
ACCOUNT_SELECT = "account.select"
ACCOUNT_SYNTHESIS = "account.synthesis"


def staffed_config() -> QORConfig:
    base = QORConfig.load()
    organization = copy.deepcopy(base.organization)
    capabilities = copy.deepcopy(base.capabilities)
    capabilities["capabilities"] = [
        *capabilities.get("capabilities", ()),
        {
            "key": ACCOUNT_RECORD,
            "name": "Record an account",
            "description": "Record the facts an objective asks the organization to keep.",
            "risk_class": "READ_ONLY",
        },
        {
            "key": ACCOUNT_SELECT,
            "name": "Select what to keep",
            "description": "Select which recorded facts the next cycle should keep.",
            "risk_class": "READ_ONLY",
        },
        {
            "key": ACCOUNT_SYNTHESIS,
            "name": "Integrate an account",
            "description": "Integrate accepted records into one account.",
            "risk_class": "ADVISORY",
        },
    ]
    for department in organization["departments"]:
        if department["key"] == "dept.governance":
            department["owns_capabilities"] = [
                *department.get("owns_capabilities", ()),
                ACCOUNT_RECORD,
                ACCOUNT_SELECT,
                ACCOUNT_SYNTHESIS,
            ]
    for agent in organization["agents"]:
        if agent["key"] == "agent.ai_cio":
            agent["capabilities"] = [
                *agent.get("capabilities", ()),
                {"capability_key": ACCOUNT_SYNTHESIS, "proficiency": 0.8},
            ]
    organization["agents"] = [
        *organization["agents"],
        {
            "key": "agent.account_keeper",
            "name": "Account Keeper",
            "kind": "AGENT",
            "roles": ["role.ai_cio"],
            "department": "dept.governance",
            "max_concurrent_assignments": 3,
            "trust_score": 0.8,
            "capabilities": [{"capability_key": ACCOUNT_RECORD, "proficiency": 0.84}],
        },
        {
            "key": "agent.account_selector",
            "name": "Account Selector",
            "kind": "AGENT",
            "roles": ["role.ai_cio"],
            "department": "dept.governance",
            "max_concurrent_assignments": 3,
            "trust_score": 0.78,
            "capabilities": [{"capability_key": ACCOUNT_SELECT, "proficiency": 0.83}],
        },
    ]
    return QORConfig(
        organization=organization,
        capabilities=capabilities,
        roles=base.roles,
        permissions=base.permissions,
        policies=base.policies,
        state_machine_dir=base.state_machine_dir,
    )
