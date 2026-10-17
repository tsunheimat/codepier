"""Strict administrative request model. Connection policy lives in permissions.py."""
from pydantic import BaseModel, ConfigDict, model_validator
from shared.util import valid_json_value

class AccessModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    @model_validator(mode='before')
    @classmethod
    def json_safe(cls, value):
        if not valid_json_value(value):
            raise ValueError('授权参数必须是有效的 UTF-8 JSON')
        return value
