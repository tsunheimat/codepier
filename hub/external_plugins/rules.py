"""Plugin action/resource pairs; backing MCP permissions remain separate."""
import json
from typing import Literal

from pydantic import Field, model_validator
from hub.access import AccessModel
from shared.util import DevError


class PluginRule(AccessModel):
    integration_id: str = Field(pattern=r'^ext_[a-f0-9]{32}$')
    scope: Literal['project', 'space'] = 'project'
    projects: list[str] = Field(default_factory=list, max_length=100)
    actions: list[str] = Field(min_length=1, max_length=32)

    @model_validator(mode='after')
    def bounded(self):
        import re
        if (len(set(self.projects)) != len(self.projects) or len(set(self.actions)) != len(self.actions)
                or any(not re.fullmatch(r'[a-z][a-z0-9_.-]{0,47}', a) for a in self.actions)
                or any(not p or len(p) > 100 or p == '*' or any(ord(c) < 32 for c in p) for p in self.projects)):
            raise ValueError('Plugin rules require explicit, unique resource/action pairs')
        if (self.scope == 'space' and self.projects) or (self.scope == 'project' and not self.projects):
            raise ValueError('Project rules need projects; space rules must not contain projects')
        return self


def allows(policy, integration_id, action, project_id):
    return any(r.integration_id == integration_id and action in r.actions
        and ((r.scope == 'space' and not project_id) or (r.scope == 'project' and project_id in r.projects))
        for r in policy.plugin_rules)


def intersect(ceiling, current):
    result = []
    for rule in ceiling.plugin_rules:
        for project in rule.projects if rule.scope == 'project' else ['']:
            actions = [a for a in rule.actions if allows(current, rule.integration_id, a, project)]
            if actions:
                result.append(PluginRule(integration_id=rule.integration_id, scope=rule.scope,
                    projects=[project] if project else [], actions=actions))
    return result


def validate_rules(store, rules, space_id):
    for rule in rules:
        row = store.one('SELECT manifest FROM external_integrations WHERE id=? AND space_id=?', (rule.integration_id, space_id))
        if not row:
            raise DevError('PLUGIN_RULE_INVALID', 'Plugin integration must belong to the current space', 400)
        actions = json.loads(row['manifest'])['actions']
        if any(a not in actions or actions[a]['scope'] != rule.scope for a in rule.actions):
            raise DevError('PLUGIN_RULE_INVALID', 'Select explicit actions with the correct project/space scope', 400)
        for project_id in rule.projects:
            if not store.one('SELECT id FROM projects WHERE id=? AND space_id=?', (project_id, space_id)):
                raise DevError('PLUGIN_RULE_INVALID', 'Plugin project must belong to the current space', 400)
