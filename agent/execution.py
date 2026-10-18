"""Target-specific process plans and a shared one-shot process supervisor."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import shutil

from agent.shell import prepare_shell, shell_denial
from agent.ssh import remote_script, ssh_argv, ssh_error, target_config
from shared.execution_policy import enforce_argv
from shared.util import DevError


@dataclass(frozen=True)
class CommandSpec:
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    inherit_env: bool
    remote: bool = False
    shell: str | None = None


class LocalBackend:
    def __init__(self, agent):
        self.agent = agent

    def plan(self, project, args, *, session=False, tty=False):
        root, local = self.agent.engine.root(project, True)
        denial = shell_denial(self.agent.config, project, local)
        if denial:
            raise DevError(*denial, 403)
        # Use the same bounded and realpath-checked directory semantics as files.
        cwd = self.agent.engine.path(root, args.get('cwd', '.'))
        if not cwd.is_dir():
            raise DevError('INVALID_CWD', 'The selected working directory is not a directory')
        shell = self.agent.config['shell']
        requested = None
        if session:
            requested = args.get('shell') or 'sh'
            if requested not in {'sh', 'bash', 'zsh'}:
                raise DevError('SESSION_SHELL_UNSUPPORTED', 'Stateful sessions currently require a POSIX shell', 409)
            executable = shutil.which(requested, path=shell['env'].get('PATH', os.environ.get('PATH')))
            if not executable:
                raise DevError('SESSION_SHELL_MISSING', 'The requested shell is not installed on this Runner', 409)
            argv = [executable, '-i' if tty else '-s']
            env = {**shell['env'], **args.get('env', {})}
        else:
            argv, _, env = prepare_shell(self.agent.config, project, local, root, args)
        return CommandSpec(argv, cwd, env, shell['inherit_env'], shell=requested)


class SSHBackend:
    def __init__(self, agent):
        self.agent = agent

    def plan(self, project, args, *, session=False, tty=False):
        target = target_config(self.agent.config, project)
        # Opening a retained shell is not a 3600-second command. The manager
        # bounds connection setup and applies the command ceiling per session.exec.
        if not session and args.get('timeout_seconds', 1) > self.agent.config['ssh']['max_timeout_seconds']:
            raise DevError('SSH_TIMEOUT_LIMIT', 'Requested timeout exceeds the Runner SSH limit')
        shell = target['shell']
        if session and args.get('shell') is not None and args['shell'] != shell:
            raise DevError('SSH_SHELL_MISMATCH', 'Use the shell configured for this saved SSH target', 409)
        if session:
            script = remote_script(args, 'exec ' + shell + (' -i' if tty else ' -s'))
        else:
            script = remote_script(args, args['command'])
        argv = ssh_argv(self.agent.config, project, tty=tty) + [shell + ' -c ' + shlex.quote(script)]
        # Remote cwd is deliberately not a project folder on the launcher.
        return CommandSpec(argv, self.agent.state_dir, dict(self.agent.config.get('shell', {}).get('env', {})),
                           True, remote=True, shell=shell)


class ProcessSupervisor:
    def __init__(self, agent):
        self.agent = agent

    async def run(self, operation_id, project, args):
        backend = SSHBackend(self.agent) if project.get('_vps_context') else LocalBackend(self.agent)
        spec = backend.plan(project, args)
        # Preserve local execution policy in addition to the Hub's resource checks.
        enforce_argv(self.agent.config, project, spec.argv, spec.cwd, spec.env)
        result = await self.agent.run_process(operation_id, spec.argv, spec.cwd, args['timeout_seconds'],
            stream=True, task_env=spec.env, full_access=True, inherit_env=spec.inherit_env, execution_project=project)
        if spec.remote:
            uncertain = bool(result.get('cancelled') or result.get('timed_out') or result.get('exit_code') in {None, 255})
            result.update(ssh_error=ssh_error(result), outcome_uncertain=uncertain,
                          remote_execution_uncertain=uncertain)
        return result
