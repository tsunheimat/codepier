"""Correlation metadata and references only. No chat bodies or task state."""
from typing import Literal
from pydantic import Field, model_validator
from shared.computer_contracts import ComputerArgs
from shared.audit_redaction import redact_text


class ResourceAssociation(ComputerArgs):
    type: Literal['project', 'vps', 'mcp']
    id: str = Field(min_length=1, max_length=100)


class OperationAssociation(ComputerArgs):
    type: Literal['native', 'mcp'] = 'native'
    id: str = Field(min_length=1, max_length=100)


class ConversationIdentity(ComputerArgs):
    platform: str = Field(min_length=1, max_length=80, pattern=r'^[a-zA-Z0-9_.-]+$')
    conversation_identifier: str = Field(min_length=1, max_length=512)
    label: str = Field(default='', max_length=160)
    original_url: str = Field(default='', max_length=2048)

    @model_validator(mode='after')
    def text_values(self):
        if any(ord(c) < 32 or ord(c) == 127 for value in (self.conversation_identifier, self.label, self.original_url) for c in value):
            raise ValueError('Conversation metadata cannot contain control characters')
        if not self.conversation_identifier.strip():
            raise ValueError('A real client-supplied identifier is required')
        if redact_text(self.conversation_identifier) != self.conversation_identifier:
            raise ValueError('Credentials are not conversation identifiers')
        self.label = redact_text(self.label)
        self.platform = self.platform.lower()
        return self


class ConversationAssociate(ConversationIdentity):
    resources: list[ResourceAssociation] = Field(default_factory=list, max_length=100)
    operations: list[OperationAssociation] = Field(default_factory=list, max_length=100)


class Conversations(ComputerArgs):
    operation: Literal['list', 'get', 'associate'] = 'list'
    conversation_id: str = Field(default='', max_length=100)
    identity: ConversationIdentity | None = None
    resources: list[ResourceAssociation] = Field(default_factory=list, max_length=100)
    operations: list[OperationAssociation] = Field(default_factory=list, max_length=100)
    resource_type: Literal['', 'project', 'vps', 'mcp'] = ''
    resource_id: str = Field(default='', max_length=100)
    limit: int = Field(default=30, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=100000)

    @model_validator(mode='after')
    def intent(self):
        if self.operation == 'get' and not self.conversation_id:
            raise ValueError('get requires conversation_id')
        if self.operation == 'associate' and not self.identity and not self.conversation_id:
            raise ValueError('associate requires a client-supplied identity or a permitted conversation_id')
        if self.operation != 'associate' and (self.identity or self.resources or self.operations):
            raise ValueError('Association metadata is only accepted for associate')
        if bool(self.resource_type) != bool(self.resource_id):
            raise ValueError('Resource filters require both type and id')
        return self


def register(Tool, tools, schemas):
    tools['conversations'] = Tool(Conversations, 'read',
        'Read this authenticated connection’s Audit session associations and associate permitted resource/operation references. '
        'Optional platform, client-supplied identifier, label and original HTTPS URL only; never stores transcripts, '
        'goals or progress. Host openai/session can correlate calls but is not a ChatGPT URL ID. '
        'Audit correlates host metadata automatically; no manual registration or URL is required. Without metadata, continue normal tool use; Audit shows unassociated activity.', local=True)
    schemas['conversations'] = {'type': 'object', 'additionalProperties': True, 'properties': {
        'conversations': {'type': 'array', 'items': {'type': 'object'}},
        'conversation': {'type': 'object'}, 'next_offset': {'type': ['integer', 'null']},
        'error': {'type': 'object', 'properties': {'code': {'type': 'string'}, 'message': {'type': 'string'}},
                  'required': ['code', 'message']}},
        'anyOf': [{'required': ['conversations', 'next_offset']}, {'required': ['conversation']}, {'required': ['error']}]}
