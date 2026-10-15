"""Explicit project/route pairs. Source association never confers device access."""
from __future__ import annotations

from pydantic import Field, model_validator
from hub.access import AccessModel
from shared.util import DevError


class RouteRule(AccessModel):
    route_id: str = Field(pattern=r'^route_[a-f0-9]{32}$')
    projects: list[str] = Field(default_factory=list, max_length=1000)
    all_projects: bool = False

    @model_validator(mode='after')
    def explicit(self):
        if not self.projects and not self.all_projects:
            raise ValueError('Select projects or explicitly consent to all present/future projects')
        if len(set(self.projects)) != len(self.projects) or any(not p or len(p) > 100 or p == '*' or any(ord(c)<32 for c in p) for p in self.projects):
            raise ValueError('Route projects must use distinct bounded IDs')
        return self


def allows(policy, route_id, project_id):
    return any(rule.route_id == route_id and (rule.all_projects or project_id in rule.projects) for rule in policy.route_rules)


def intersect(ceiling, current):
    result = []
    for rule in ceiling.route_rules:
        if rule.all_projects:
            raise ValueError('Fixed route snapshots cannot include future-project selectors')
        projects = [p for p in rule.projects if allows(current, rule.route_id, p)]
        if projects:
            result.append(RouteRule(route_id=rule.route_id, projects=projects))
    return result


def validate_rules(store, rules, space_id):
    for rule in rules:
        if not store.one('SELECT id FROM action_routes WHERE id=? AND space_id=?', (rule.route_id, space_id)):
            raise DevError('ROUTE_RULE_INVALID', 'Choose an execution route in the current space', 400)
        for project in rule.projects:
            if not store.one('SELECT id FROM projects WHERE id=? AND space_id=?', (project, space_id)):
                raise DevError('ROUTE_RULE_INVALID', 'Choose projects in the current space', 400)
