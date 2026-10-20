"""Connection-bound resource tools. Neither tool arguments nor labels select a Space."""
from typing import Literal
from pydantic import Field
from shared.workspace_contracts import WorkspaceModel, ProjectInput

WORKSPACE_TOOLS=frozenset({'sources_list','projects_create','get_execution_options'})


class ProjectCreate(ProjectInput):
    idempotency_key: str=Field(min_length=8,max_length=128)
    expected_version: Literal[0]=0


class ExecutionOptions(WorkspaceModel):
    project: str=Field(min_length=1,max_length=100)
    action: Literal['shell','git','test']='shell'


def register(Tool,Empty,Model,tools,schemas):
    tools['sources_list']=Tool(Empty,'read',
        'List only workspace sources this connection may use when the user requests project creation. '
        'This is not required for ordinary project discovery, file access or command execution.',local=True)
    tools['projects_create']=Tool(ProjectCreate,'projects.create',
        'Create one new Project from an authorized workspace source and bounded relative subpath. Root dot is allowed. '
        'Cannot select global devices, add mounts, create filesystem directories or modify permissions. '
        'Resume a pending validation with the same arguments and idempotency key.',True,True)
    tools['get_execution_options']=Tool(ExecutionOptions,'execute',
        'Only when the USER explicitly asks about execution locations or requests a different machine: '
        'list this project source’s allowed execution devices and structured path mappings. '
        'Do NOT call as a preflight for ordinary commands or automatically after failure. '
        'Ordinary exec omits execution_device_id and uses the configured default without this call.',False,True)
    schemas['sources_list']={'type':'object','properties':{'sources':{'type':'array','items':{'type':'object'}}},'required':['sources']}
    schemas['projects_create']={'type':'object','properties':{'id':{'type':'string'},'alias':{'type':'string'},
        'description':{'type':'string'},'version':{'type':'integer'},'accessible':{'type':'boolean'}},'required':['id','alias']}
    schemas['get_execution_options']={'type':'object','properties':{'project':{'type':'string'},'action':{'type':'string'},
        'default_device_id':{'type':['string','null']},'devices':{'type':'array','items':{'type':'object'}},
        'logical_project_root':{'type':'string'},'path_usage':{'type':'string'},'selection_usage':{'type':'string'}},
        'required':['project','default_device_id','devices','path_usage']}
