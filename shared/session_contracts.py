"""Local/SSH × one-shot/persistent/PTY contracts; no duplicate SSH tool facade."""
from typing import Literal
from pydantic import Field, model_validator
from shared.workspace_contracts import WorkspaceModel


class Session(WorkspaceModel):
    operation: Literal['open','exec','read','write','resize','signal','status','close']
    session_id: str=Field(default='',pattern=r'^(|[a-f0-9]{32})$')
    project: str=Field(default='',max_length=100)
    workspace_id: str=Field(default='',pattern=r'^(|[a-f0-9]{32})$')
    target: str=Field(default='agent',pattern=r'^(agent|vps:[a-f0-9]{32})$')
    execution_device_id: str | None=Field(default=None,pattern=r'^[A-Za-z0-9_-]{1,100}$',
        description='Only for user-requested device selection on open. Omit for the configured default.')
    mode: Literal['persistent_shell','pty']='persistent_shell'
    shell: Literal['sh','bash','zsh','cmd','powershell'] | None = Field(default=None,
        description='Omit to use the saved SSH target shell or the local Runner default. An explicit SSH shell must match its saved target.')
    cwd: str=Field(default='.',min_length=1,max_length=4096)
    env: dict[str,str]=Field(default_factory=dict,max_length=128)
    command: str=Field(default='',max_length=65536)
    data: str=Field(default='',max_length=65536)
    data_base64: str=Field(default='',max_length=87384)
    cursor: int=Field(default=0,ge=0)
    limit: int=Field(default=32768,ge=1,le=65536)
    rows: int=Field(default=24,ge=1,le=1000)
    cols: int=Field(default=80,ge=1,le=2000)
    signal: Literal['interrupt','terminate','kill']='interrupt'
    timeout_seconds: int=Field(default=3600,ge=1,le=86400)
    yield_seconds: int=Field(default=1,ge=0,le=10)
    idempotency_key: str | None=Field(default=None,min_length=8,max_length=128)

    @model_validator(mode='after')
    def operation_fields(self):
        public={'operation','session_id','yield_seconds','idempotency_key'}
        allowed={
            'open':{'project','workspace_id','target','execution_device_id','mode','shell','cwd','env','rows','cols'},
            'exec':{'command','timeout_seconds'},'read':{'cursor','limit'},'write':{'data','data_base64'},
            'resize':{'rows','cols'},'signal':{'signal'},'status':set(),'close':set(),
        }
        unexpected={name for name in self.model_fields_set - public - allowed[self.operation]
                    if getattr(self,name) != type(self).model_fields[name].get_default(call_default_factory=True)}
        if unexpected:
            raise ValueError('This operation does not accept '+', '.join(sorted(unexpected)))
        if self.operation=='open':
            if self.session_id or self.target=='agent' and not self.project:
                raise ValueError('Open needs a local project or a saved VPS target, not a session_id')
            if self.target!='agent' and (self.workspace_id or self.execution_device_id):
                raise ValueError('SSH Sessions use their saved Runner, not a project worktree/device override')
        elif not self.session_id:
            raise ValueError('Use the original session_id; a new Session does not resume the old Shell state')
        if self.operation not in {'read','status'} and not self.idempotency_key:
            raise ValueError('Mutations require an idempotency key; repeating input is not recovery')
        if self.operation=='exec' and not self.command:
            raise ValueError('A persistent-shell command cannot be empty')
        if self.operation=='write':
            if bool(self.data) == bool(self.data_base64):
                raise ValueError('PTY input needs exactly one of data or data_base64')
            if self.data_base64:
                import base64
                decoded = base64.b64decode(self.data_base64, validate=True)
                if not 1 <= len(decoded) <= 65536:
                    raise ValueError('PTY input must contain 1–65536 bytes')
        if any('\x00' in value for value in (self.command,self.cwd,*self.env.keys(),*self.env.values())):
            raise ValueError('Command, cwd and environment cannot contain NUL')
        import re
        if any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',key) for key in self.env):
            raise ValueError('Invalid environment variable name')
        return self


def register(Tool,tools,schemas):
    tools['session']=Tool(Session,'execute',
        'Open or operate one persistent Shell or interactive PTY on a project’s default device or a saved VPS. '
        'Sessions keep their original target. Persistent exec commands share cwd/env and are serialized. '
        'PTY read/write exchanges terminal bytes (UTF-8 text may contain escapes); it is not per-command exit status. '
        'Use original session/operation IDs and idempotency keys to observe uncertain input; never open a replacement pretending to restore state. '
        'Ordinary opens omit shell to use the saved target/Runner default. '
        'Ordinary local opens omit execution_device_id; choose another only when the user explicitly requests it.',True,False)
    schemas['session']={'type':'object','properties':{'session_id':{'type':'string'},'operation_id':{'type':'string'},
        'mode':{'type':'string'},'state':{'type':'string'},'pending':{'type':'boolean'},'result':{'type':'object'},
        'cursor':{'type':'integer'},'data':{'type':'string'},'data_base64':{'type':'string'},
        'lost_bytes':{'type':'integer'},'exit_code':{'type':['integer','null']}}}
