from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator
from shared.util import valid_json_value


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def json_safe(cls, value):
        if not valid_json_value(value):
            raise ValueError("参数必须是有效的 UTF-8 JSON，且不含非有限数值或过深嵌套")
        return value


class ComputerDecision(Model):
    action: str = Field(pattern=r"^(accept|decline|cancel)$")


class Login(Model):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class DeviceCreate(Model):
    name: str = Field(min_length=1, max_length=80)
    hub_url: str = Field(min_length=1, max_length=500)


class DeviceUpdate(Model):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    enabled: bool | None = None


class BuiltinRootInput(Model):
    path: str = Field(min_length=1, max_length=2048)
    writable: bool


class BuiltinRootsInput(Model):
    roots: list[BuiltinRootInput] = Field(max_length=32)
    revision: int = Field(ge=0, le=2**53 - 1)




class ToolCall(Model):
    tool: str = Field(min_length=1, max_length=100)
    arguments: dict = Field(default_factory=dict)





class PasswordInput(Model):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class SettingsInput(Model):
    public_url: str = Field(min_length=1, max_length=500)
