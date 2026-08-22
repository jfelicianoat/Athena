"""Controlled local command execution.

Nothing here spawns a shell. A command is parsed into argv, classified by a
deterministic policy that inspects the executable, its arguments and the working
directory, and only then handed to the PermissionEngine. The AgentLoop never calls
subprocess itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from athena.cancellation import CancellationToken
from athena.errors import (
    ProcessCancelledError,
    ProcessTimeoutError,
    ToolExecutionError,
    ToolValidationError,
)
from athena.events import EventBus, EventName, ProcessEvent
from athena.permissions import PermissionRequest, RiskLevel, RiskTier
from athena.tools import ToolContext, ToolResult, ToolSpec
from athena.types import JSONObject

#: Characters that would hand control back to a shell interpreter.
_SHELL_METACHARACTERS = (";", "&", "|", ">", "<", "`", "$(", "\n", "\r", "((")

_MAX_OUTPUT_CHARS = 20_000

#: Executables that only inspect local state.
_READ_COMMANDS = frozenset(
    {"ls", "dir", "cat", "head", "tail", "wc", "echo", "pwd", "find", "grep", "rg", "tree"}
)

#: Executables that build or verify locally, writing only inside their own artefacts.
_BUILD_COMMANDS = frozenset(
    {"pytest", "ruff", "mypy", "black", "flake8", "tsc", "eslint", "prettier", "make", "dotnet"}
)

_SUBCOMMAND_POLICY: dict[str, dict[str, str]] = {
    "git": {
        "status": "read",
        "diff": "read",
        "log": "read",
        "show": "read",
        "branch": "read",
        "remote": "read",
        "rev-parse": "read",
        "ls-files": "read",
        "blame": "read",
        "commit": "ask",
        "add": "ask",
        "stash": "ask",
        "checkout": "ask",
        "restore": "ask",
        "tag": "ask",
        "push": "forbidden",
        "pull": "forbidden",
        "fetch": "forbidden",
        "merge": "forbidden",
        "rebase": "forbidden",
        "reset": "forbidden",
        "clean": "forbidden",
        "submodule": "forbidden",
    },
    "python": {"-m": "module", "-c": "forbidden"},
    "python3": {"-m": "module", "-c": "forbidden"},
    "npm": {"test": "build", "run": "build", "ci": "ask", "install": "ask", "publish": "forbidden"},
    "pnpm": {"test": "build", "run": "build", "install": "ask", "publish": "forbidden"},
    "yarn": {"test": "build", "run": "build", "install": "ask", "publish": "forbidden"},
    "pip": {"list": "read", "show": "read", "freeze": "read", "install": "ask", "uninstall": "ask"},
    "uv": {"run": "build", "pip": "ask", "sync": "ask", "add": "ask", "publish": "forbidden"},
    "cargo": {"build": "build", "test": "build", "check": "build", "publish": "forbidden"},
    "go": {"build": "build", "test": "build", "vet": "build"},
    "docker": {"ps": "read", "images": "read"},
}

#: Modules that are safe to run through `python -m`.
_ALLOWED_PYTHON_MODULES = frozenset({"pytest", "ruff", "mypy", "unittest", "compileall", "json"})

_ASK_COMMANDS = frozenset({"rm", "del", "mv", "move", "cp", "copy", "chmod", "chown", "mkdir"})

_FORBIDDEN_COMMANDS = frozenset(
    {
        "sudo",
        "su",
        "doas",
        "runas",
        "curl",
        "wget",
        "nc",
        "netcat",
        "ssh",
        "scp",
        "sftp",
        "rsync",
        "ftp",
        "telnet",
        "shutdown",
        "reboot",
        "halt",
        "poweroff",
        "mkfs",
        "fdisk",
        "format",
        "diskpart",
        "sh",
        "bash",
        "zsh",
        "cmd",
        "powershell",
        "pwsh",
        "eval",
        "exec",
        "source",
        "kill",
        "killall",
        "taskkill",
        "reg",
        "regedit",
        "schtasks",
        "at",
        "crontab",
        "terraform",
        "kubectl",
        "helm",
        "aws",
        "gcloud",
        "az",
    }
)


@dataclass(frozen=True, slots=True)
class CommandClassification:
    tier: RiskTier
    risk: RiskLevel
    category: str
    reason: str
    effects: tuple[str, ...]
    concurrency_safe: bool


class CommandPolicy:
    """Deterministic classification of a parsed command into a capability tier.

    The built-in tables cover common development commands. A deployment can classify
    additional executables without editing this module, but it can never reclassify a
    forbidden one: the deny list is checked first and always wins.
    """

    def __init__(
        self,
        *,
        read_commands: Iterable[str] = (),
        build_commands: Iterable[str] = (),
        ask_commands: Iterable[str] = (),
        forbidden_commands: Iterable[str] = (),
        subcommands: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        self.read_commands = _READ_COMMANDS | frozenset(read_commands)
        self.build_commands = _BUILD_COMMANDS | frozenset(build_commands)
        self.ask_commands = _ASK_COMMANDS | frozenset(ask_commands)
        self.forbidden_commands = _FORBIDDEN_COMMANDS | frozenset(forbidden_commands)
        merged = {name: dict(policy) for name, policy in _SUBCOMMAND_POLICY.items()}
        for name, policy in (subcommands or {}).items():
            merged.setdefault(name, {}).update(policy)
        self.subcommands = merged

    def classify(self, argv: tuple[str, ...], cwd: str) -> CommandClassification:
        executable = Path(argv[0]).name.lower()
        executable = executable.removesuffix(".exe")
        arguments = argv[1:]

        if executable in self.forbidden_commands:
            return self._forbidden(f"{executable} is outside the local execution policy")

        subcommands = self.subcommands.get(executable)
        if subcommands is not None:
            return self._classify_subcommand(executable, arguments, subcommands, cwd)
        if executable in self.read_commands:
            return self._read(executable, cwd)
        if executable in self.build_commands:
            return self._build(executable, cwd)
        if executable in self.ask_commands:
            return CommandClassification(
                RiskTier.R3_EXTERNAL_OR_IRREVERSIBLE,
                RiskLevel.HIGH,
                "mutating",
                f"{executable} can irreversibly change files on disk",
                (f"Runs {executable} in {cwd}", "May delete or move files"),
                concurrency_safe=False,
            )
        return self._forbidden(f"{executable} is not covered by the execution policy")

    def _classify_subcommand(
        self,
        executable: str,
        arguments: tuple[str, ...],
        subcommands: Mapping[str, str],
        cwd: str,
    ) -> CommandClassification:
        first = next((item for item in arguments if not item.startswith("-")), None)
        flag = next((item for item in arguments if item in subcommands), None)
        key = flag if flag in ("-m", "-c") else first
        if key is None:
            return self._read(executable, cwd)
        policy = subcommands.get(key)
        if policy is None:
            return self._forbidden(f"{executable} {key} is not covered by the execution policy")
        if policy == "module":
            return self._classify_python_module(executable, arguments, cwd)
        if policy == "forbidden":
            return self._forbidden(f"{executable} {key} is forbidden")
        if policy == "read":
            return self._read(f"{executable} {key}", cwd)
        if policy == "build":
            return self._build(f"{executable} {key}", cwd)
        return CommandClassification(
            RiskTier.R3_EXTERNAL_OR_IRREVERSIBLE,
            RiskLevel.HIGH,
            "irreversible",
            f"{executable} {key} changes state that Athena cannot undo",
            (
                f"Runs {executable} {key} in {cwd}",
                "May install dependencies, migrate data, or record a commit",
            ),
            concurrency_safe=False,
        )

    def _classify_python_module(
        self, executable: str, arguments: tuple[str, ...], cwd: str
    ) -> CommandClassification:
        try:
            module = arguments[arguments.index("-m") + 1]
        except (ValueError, IndexError):
            return self._forbidden(f"{executable} -m requires a module name")
        if module.split(".")[0] not in _ALLOWED_PYTHON_MODULES:
            return self._forbidden(f"python -m {module} is not covered by the execution policy")
        return self._build(f"python -m {module}", cwd)

    @staticmethod
    def _read(label: str, cwd: str) -> CommandClassification:
        return CommandClassification(
            RiskTier.R2_LOCAL_EXECUTION,
            RiskLevel.LOW,
            "read",
            f"{label} only inspects local state",
            (f"Runs {label} in {cwd}", "Reads local state without writing"),
            concurrency_safe=True,
        )

    @staticmethod
    def _build(label: str, cwd: str) -> CommandClassification:
        return CommandClassification(
            RiskTier.R2_LOCAL_EXECUTION,
            RiskLevel.MEDIUM,
            "build",
            f"{label} builds or verifies locally",
            (
                f"Runs {label} in {cwd}",
                "May write caches or build artefacts inside the workspace",
            ),
            concurrency_safe=False,
        )

    @staticmethod
    def _forbidden(reason: str) -> CommandClassification:
        return CommandClassification(
            RiskTier.R4_FORBIDDEN,
            RiskLevel.CRITICAL,
            "forbidden",
            reason,
            ("Refused before execution",),
            concurrency_safe=False,
        )


def parse_command(command: str) -> tuple[str, ...]:
    """Split a command into argv, refusing anything that needs a shell to interpret."""
    if not command.strip():
        raise ToolValidationError("command must be a non-empty string")
    for token in _SHELL_METACHARACTERS:
        if token in command:
            raise ToolValidationError(
                f"command contains the shell metacharacter {token!r}; "
                "issue one plain command per call"
            )
    try:
        argv = _split(command)
    except ValueError as exc:
        raise ToolValidationError(f"command could not be parsed: {exc}") from exc
    if not argv:
        raise ToolValidationError("command must contain an executable")
    return tuple(argv)


def _split(command: str) -> list[str]:
    """Split into argv without destroying Windows paths.

    POSIX-mode shlex treats a backslash as an escape, so an unquoted `C:\repo\run.py`
    would silently collapse to `C:reporun.py`. On Windows the backslash is a separator,
    so tokens are split in non-POSIX mode and unwrapped afterwards.
    """
    if sys.platform != "win32":
        return shlex.split(command, posix=True)
    return [_unwrap(token) for token in shlex.split(command, posix=False)]


def _unwrap(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'"):
        return token[1:-1]
    return token


def _terminate_tree(process: asyncio.subprocess.Process) -> None:
    """Kill the child and everything it spawned, so no orphan survives a cancel."""
    if process.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
                timeout=10,
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()


async def _spawn_process(
    argv: tuple[str, ...], cwd: Path, env: Mapping[str, str] | None = None
) -> asyncio.subprocess.Process:
    """Start a child with pipes and no shell, isolated into its own group on POSIX."""
    environment = {**os.environ, **env} if env else None
    try:
        if sys.platform == "win32":
            return await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                env=environment,
            )
        return await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            env=environment,
        )
    except (OSError, ValueError) as exc:
        raise ToolExecutionError(
            f"Cannot start command: {argv[0]}", details={"argv": list(argv)}
        ) from exc


async def run_process(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    timeout_seconds: float,
    cancellation: CancellationToken,
    env: Mapping[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run one argv with a mandatory timeout, killing the tree on cancel or timeout."""
    cancellation.raise_if_cancelled()
    process = await _spawn_process(argv, cwd, env)
    unsubscribe = cancellation.register(lambda: _terminate_tree(process))
    try:
        raw_out, raw_err = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except (TimeoutError, asyncio.CancelledError):
        _terminate_tree(process)
        await process.wait()
        if cancellation.is_cancelled:
            raise ProcessCancelledError("Command cancelled and child terminated") from None
        raise ProcessTimeoutError(
            f"Command exceeded {timeout_seconds} seconds",
            details={"argv": list(argv)},
        ) from None
    finally:
        unsubscribe()
    if cancellation.is_cancelled:
        raise ProcessCancelledError("Command cancelled and child terminated")
    return (
        process.returncode or 0,
        raw_out.decode("utf-8", errors="replace"),
        raw_err.decode("utf-8", errors="replace"),
    )


class BashTool:
    """Runs one policy-approved command, with a mandatory timeout and hard cancellation."""

    def __init__(
        self,
        policy: CommandPolicy | None = None,
        event_bus: EventBus | None = None,
        *,
        default_timeout_seconds: float = 30.0,
        max_timeout_seconds: float = 600.0,
    ) -> None:
        if default_timeout_seconds <= 0 or max_timeout_seconds <= 0:
            raise ValueError("Timeouts must be positive")
        self.policy = policy or CommandPolicy()
        self.event_bus = event_bus
        self.default_timeout_seconds = default_timeout_seconds
        self.max_timeout_seconds = max_timeout_seconds

    spec = ToolSpec(
        name="bash",
        description=(
            "Run one local command without a shell. Metacharacters are rejected; "
            "the permission engine classifies the executable, its arguments and the cwd."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string", "default": "."},
                "timeout_seconds": {"type": "number", "minimum": 1},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "argv": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string"},
                "exit_code": {"type": "integer"},
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
                "stdout_truncated": {"type": "boolean"},
                "stderr_truncated": {"type": "boolean"},
                "duration_seconds": {"type": "number"},
            },
            "required": ["argv", "cwd", "exit_code", "stdout", "stderr"],
            "additionalProperties": False,
        },
        risk=RiskLevel.HIGH,
        max_result_size_chars=16_000,
        search_hint="run a local verification such as the test suite",
    )

    def validate(self, arguments: JSONObject) -> JSONObject:
        unknown = set(arguments) - {"command", "cwd", "timeout_seconds"}
        if unknown:
            raise ToolValidationError(f"Unknown input fields: {', '.join(sorted(unknown))}")
        command = arguments.get("command")
        if not isinstance(command, str):
            raise ToolValidationError("command must be a string")
        parse_command(command)
        cwd = arguments.get("cwd", ".")
        if not isinstance(cwd, str) or not cwd:
            raise ToolValidationError("cwd must be a non-empty string")
        return {
            "command": command,
            "cwd": cwd,
            "timeout_seconds": self._timeout(arguments),
        }

    def _timeout(self, arguments: JSONObject) -> float:
        raw = arguments.get("timeout_seconds", self.default_timeout_seconds)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ToolValidationError("timeout_seconds must be a number")
        if raw <= 0 or raw > self.max_timeout_seconds:
            raise ToolValidationError(
                f"timeout_seconds must be between 0 and {self.max_timeout_seconds}"
            )
        return float(raw)

    def _classify(self, context: ToolContext, arguments: JSONObject) -> CommandClassification:
        command = arguments.get("command")
        if not isinstance(command, str):
            raise ToolValidationError("command must be a string")
        argv = parse_command(command)
        cwd = arguments.get("cwd", ".")
        directory = context.workspace.resolve(cwd if isinstance(cwd, str) else ".")
        return self.policy.classify(argv, context.workspace.relative(directory))

    def is_read_only(self, arguments: JSONObject) -> bool:
        try:
            argv = parse_command(str(arguments.get("command", "")))
        except ToolValidationError:
            return False
        return self.policy.classify(argv, ".").category == "read"

    def is_destructive(self, arguments: JSONObject) -> bool:
        try:
            argv = parse_command(str(arguments.get("command", "")))
        except ToolValidationError:
            return True
        return self.policy.classify(argv, ".").category in ("mutating", "irreversible", "forbidden")

    def is_concurrency_safe(self, arguments: JSONObject) -> bool:
        """Explicit per-command classification; execution is never assumed safe."""
        try:
            argv = parse_command(str(arguments.get("command", "")))
        except ToolValidationError:
            return False
        return self.policy.classify(argv, ".").concurrency_safe

    def permission(self, context: ToolContext, arguments: JSONObject) -> PermissionRequest:
        classification = self._classify(context, arguments)
        command = str(arguments.get("command", ""))
        cwd = str(arguments.get("cwd", "."))
        return PermissionRequest(
            tool_name=self.spec.name,
            operation="run_command",
            action=f"run `{command}` in {cwd}",
            workspace=context.workspace,
            risk=classification.risk,
            tier=classification.tier,
            is_read_only=classification.category == "read",
            is_destructive=classification.category in ("mutating", "irreversible"),
            is_concurrency_safe=classification.concurrency_safe,
            reason=classification.reason,
            possible_effects=classification.effects,
            resources=(command,),
            arguments=arguments,
        )

    async def execute(
        self,
        context: ToolContext,
        arguments: JSONObject,
        cancellation: CancellationToken,
    ) -> ToolResult:
        cancellation.raise_if_cancelled()
        argv = parse_command(str(arguments.get("command", "")))
        cwd = arguments.get("cwd", ".")
        directory = context.workspace.resolve(cwd if isinstance(cwd, str) else ".")
        if not directory.is_dir():
            raise ToolValidationError(f"cwd is not a directory: {cwd}")
        timeout = self._timeout(arguments)
        started = time.monotonic()
        process = await _spawn_process(argv, directory)
        await self._publish(
            EventName.PROCESS_STARTED,
            context,
            {"pid": process.pid, "argv": list(argv), "cwd": context.workspace.relative(directory)},
        )
        unsubscribe = cancellation.register(lambda: _terminate_tree(process))
        try:
            stdout, stderr, timed_out = await self._collect(process, timeout)
        finally:
            unsubscribe()
        duration = round(time.monotonic() - started, 3)
        if cancellation.is_cancelled:
            await self._publish(
                EventName.PROCESS_CANCELLED,
                context,
                {"pid": process.pid, "duration_seconds": duration},
            )
            raise ProcessCancelledError(
                "Command cancelled and child process terminated",
                details={"argv": list(argv)},
            )
        if timed_out:
            await self._publish(
                EventName.PROCESS_FAILED,
                context,
                {"pid": process.pid, "reason": "timeout", "duration_seconds": duration},
            )
            raise ProcessTimeoutError(
                f"Command exceeded {timeout} seconds and was terminated",
                details={"argv": list(argv), "timeout_seconds": timeout},
            )
        exit_code = process.returncode
        await self._publish(
            EventName.PROCESS_COMPLETED,
            context,
            {"pid": process.pid, "exit_code": exit_code, "duration_seconds": duration},
        )
        return ToolResult(
            {
                "argv": list(argv),
                "cwd": context.workspace.relative(directory),
                "exit_code": exit_code,
                "stdout": stdout[:_MAX_OUTPUT_CHARS],
                "stderr": stderr[:_MAX_OUTPUT_CHARS],
                "stdout_truncated": len(stdout) > _MAX_OUTPUT_CHARS,
                "stderr_truncated": len(stderr) > _MAX_OUTPUT_CHARS,
                "duration_seconds": duration,
            }
        )

    @staticmethod
    async def _collect(
        process: asyncio.subprocess.Process, timeout: float
    ) -> tuple[str, str, bool]:
        try:
            raw_out, raw_err = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except (TimeoutError, asyncio.CancelledError):
            _terminate_tree(process)
            await process.wait()
            return "", "", True
        return (
            raw_out.decode("utf-8", errors="replace"),
            raw_err.decode("utf-8", errors="replace"),
            False,
        )

    async def _publish(self, name: EventName, context: ToolContext, payload: JSONObject) -> None:
        if self.event_bus is None:
            return
        await self.event_bus.publish(
            ProcessEvent(name, context.session_id, payload, context.call_id)
        )
