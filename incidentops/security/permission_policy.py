from __future__ import annotations

from incidentops.db.models import ProjectRole

ROLE_ORDER = {
    ProjectRole.viewer: 0,
    ProjectRole.investigator: 1,
    ProjectRole.approver: 2,
    ProjectRole.admin: 3,
}


def role_allows(actual: ProjectRole, minimum: ProjectRole) -> bool:
    return ROLE_ORDER[actual] >= ROLE_ORDER[minimum]
