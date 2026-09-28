"""Controlled local command execution.

Nothing here spawns a shell. A command is parsed into argv, classified by a
deterministic policy that inspects the executable, its arguments and the working
directory, and only then handed to the PermissionEngine. The AgentLoop never calls
subprocess itself.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import partial
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
from athena.process_tree import (
    communicate_bounded,
    contain,
    reap,
    release,
    terminate_tree,
)
from athena.tools import ToolContext, ToolResult, ToolSpec
from athena.types import JSONObject

#: Characters that would hand control back to a shell interpreter.
_SHELL_METACHARACTERS = (";", "&", "|", ">", "<", "`", "$(", "\n", "\r", "((")

_MAX_OUTPUT_CHARS = 20_000

#: Executables that only inspect local state.
_READ_COMMANDS = frozenset(
    {"ls", "dir", "cat", "head", "tail", "wc", "echo", "pwd", "find", "grep", "rg", "tree"}
)

#: Executables that build or verify locally. They run code the project defines (tests,
#: build files, plugins), which is why they are execution and never "read".
_BUILD_COMMANDS = frozenset(
    {"pytest", "ruff", "mypy", "black", "flake8", "tsc", "eslint", "prettier", "make"}
)

#: Scripts of a package manager that are conventionally verification. Any other script
#: name is arbitrary project code: `npm run deploy` is as much a build as `rm` is a read.
_VERIFICATION_SCRIPTS = frozenset(
    {"test", "tests", "lint", "build", "check", "typecheck", "type-check", "format:check"}
)

_SUBCOMMAND_POLICY: dict[str, dict[str, str]] = {
    "git": {
        "status": "read",
        "diff": "read",
        "log": "read",
        "show": "read",
        "branch": "git_branch",
        "remote": "git_remote",
        "rev-parse": "read",
        "ls-files": "read",
        "blame": "read",
        "commit": "ask",
        "add": "ask",
        "stash": "ask",
        "checkout": "ask",
        "switch": "ask",
        "restore": "ask",
        # Etiquetar no es una capacidad de Athena (security-model.md): se anuncia fuera y
        # se clasifica fuera. Antes era R3 y el documento decia que no existia.
        "tag": "forbidden",
        "push": "forbidden",
        "pull": "forbidden",
        "fetch": "forbidden",
        "clone": "forbidden",
        "merge": "forbidden",
        "rebase": "forbidden",
        "reset": "forbidden",
        "clean": "forbidden",
        "submodule": "forbidden",
        "config": "forbidden",
        "worktree": "forbidden",
        "gc": "forbidden",
        "filter-branch": "forbidden",
        "update-ref": "forbidden",
    },
    "python": {"-m": "module", "-c": "forbidden"},
    "python3": {"-m": "module", "-c": "forbidden"},
    "py": {"-m": "module", "-c": "forbidden"},
    "npm": {
        "test": "build",
        "run": "script",
        "run-script": "script",
        "ci": "ask",
        "install": "ask",
        "publish": "forbidden",
        "exec": "forbidden",
        "x": "forbidden",
    },
    "pnpm": {
        "test": "build",
        "run": "script",
        "install": "ask",
        "publish": "forbidden",
        "exec": "forbidden",
        "dlx": "forbidden",
    },
    "yarn": {
        "test": "build",
        "run": "script",
        "install": "ask",
        "publish": "forbidden",
        "dlx": "forbidden",
    },
    "pip": {"list": "read", "show": "read", "freeze": "read", "install": "ask", "uninstall": "ask"},
    "uv": {
        "run": "wrapper",
        "pip": "ask",
        "sync": "ask",
        "add": "ask",
        "publish": "forbidden",
        "tool": "forbidden",
    },
    "cargo": {"build": "build", "test": "build", "check": "build", "publish": "forbidden"},
    "go": {"build": "build", "test": "build", "vet": "build"},
    "dotnet": {"build": "build", "test": "build", "nuget": "forbidden", "publish": "forbidden"},
    "docker": {"ps": "read", "images": "read"},
}

#: How strict each policy word is. An extension may add a subcommand, or make an existing
#: one stricter; it may never make a built-in one looser. Before, merging the tables let
#: `subcommands={"git": {"push": "read"}}` turn `git push` into R2 (A02).
_STRICTNESS = {
    "read": 0,
    "build": 1,
    "module": 1,
    "script": 1,
    "wrapper": 1,
    "git_branch": 1,
    "git_remote": 1,
    "ask": 2,
    "forbidden": 3,
}

#: Modules that are safe to run through `python -m`.
_ALLOWED_PYTHON_MODULES = frozenset({"pytest", "ruff", "mypy", "unittest", "compileall", "json"})

_ASK_COMMANDS = frozenset({"rm", "del", "mv", "move", "cp", "copy", "chmod", "chown", "mkdir"})

#: git options that change where git looks or what it executes. `-c core.pager=...` or
#: `-c diff.external=...` is a command chosen by whoever wrote the argument.
_GIT_GLOBAL_FORBIDDEN = frozenset(
    {"-c", "--config-env", "--exec-path", "--git-dir", "--work-tree", "--namespace"}
)
_GIT_GLOBAL_WITH_VALUE = frozenset({"-C"})

#: Flags that make an otherwise inspecting or verifying command write files.
_WRITING_FLAGS: dict[str, frozenset[str]] = {
    "ruff": frozenset({"--fix", "--unsafe-fixes", "--add-noqa"}),
    "eslint": frozenset({"--fix", "--fix-dry-run"}),
    "prettier": frozenset({"--write", "-w"}),
    "tree": frozenset({"-o"}),
    "git": frozenset({"--output", "-o"}),
}

#: Flags that make a reading command execute or delete something else.
_EXECUTING_FLAGS: dict[str, frozenset[str]] = {
    "find": frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fls"}),
    "rg": frozenset({"--pre", "--pre-glob"}),
    "git": frozenset({"--ext-diff", "--textconv"}),
}

#: `ruff format` and `black` rewrite files unless asked only to check.
_CHECK_ONLY_FLAGS = frozenset({"--check", "--diff"})

#: Options of `uv run` that take a value, so the wrapped command starts after them.
_UV_RUN_OPTIONS_WITH_VALUE = frozenset(
    {
        "--with",
        "--with-requirements",
        "--with-editable",
        "--python",
        "-p",
        "--project",
        "--directory",
        "--package",
        "--extra",
        "--group",
        "--env-file",
        "--index",
        "--index-url",
        "--default-index",
    }
)

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
        "npx",
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
    additional executables without editing this module, but it can never loosen a
    built-in rule: the deny list is checked first and always wins, and an override of a
    built-in subcommand only takes effect when it is stricter.

    What this is and what it is not: it classifies argv and confines every argument that
    names a path to the workspace. It does not sandbox the process. A test suite or a
    build script runs with the user's permissions once it is authorized, which is why all
    of them are execution (R2) and need that authority explicitly.
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
        self.forbidden_commands = _FORBIDDEN_COMMANDS | frozenset(forbidden_commands)
        self.read_commands = (_READ_COMMANDS | frozenset(read_commands)) - self.forbidden_commands
        self.build_commands = (
            _BUILD_COMMANDS | frozenset(build_commands)
        ) - self.forbidden_commands
        self.ask_commands = (_ASK_COMMANDS | frozenset(ask_commands)) - self.forbidden_commands
        merged = {name: dict(policy) for name, policy in _SUBCOMMAND_POLICY.items()}
        for name, policy in (subcommands or {}).items():
            table = merged.setdefault(name, {})
            for key, value in policy.items():
                if value not in _STRICTNESS:
                    raise ValueError(f"Unknown command policy {value!r} for {name} {key}")
                current = table.get(key)
                if current is not None and _STRICTNESS[value] < _STRICTNESS[current]:
                    # Una extension no puede relajar lo incorporado: se ignora en vez de
                    # aplicarse a medias. Lanzar romperia un despliegue por una linea de
                    # configuracion; aplicarlo convertiria `git push` en una lectura.
                    continue
                table[key] = value
        self.subcommands = merged

    def classify(
        self,
        argv: tuple[str, ...],
        cwd: str,
        *,
        workspace_root: Path | None = None,
        cwd_path: Path | None = None,
    ) -> CommandClassification:
        executable = Path(argv[0]).name.lower()
        executable = executable.removesuffix(".exe")
        arguments = argv[1:]

        if executable in self.forbidden_commands:
            return self._forbidden(f"{executable} is outside the local execution policy")
        if workspace_root is not None:
            escaping = _escaping_argument(arguments, workspace_root, cwd_path or workspace_root)
            if escaping is not None:
                return self._forbidden(
                    f"argument {escaping!r} points outside the workspace; commands may only "
                    "name paths inside it"
                )
        executes = _EXECUTING_FLAGS.get(executable, frozenset())
        # `find` usa opciones largas con un solo guion (`-delete`): se mira la palabra
        # entera ademas de su forma corta.
        hit = next(
            (item for item in arguments if item in executes or _flag_name(item) in executes),
            None,
        )
        if hit is not None:
            return self._forbidden(f"{executable} {hit} runs or deletes arbitrary files")

        subcommands = self.subcommands.get(executable)
        if subcommands is not None:
            classification = self._classify_subcommand(
                executable, arguments, subcommands, cwd, workspace_root, cwd_path
            )
        elif executable in self.read_commands:
            classification = self._read(executable, cwd)
        elif executable in self.build_commands:
            classification = self._build(executable, cwd)
        elif executable in self.ask_commands:
            return self._mutating(executable, cwd, f"{executable} can irreversibly change files")
        else:
            return self._forbidden(f"{executable} is not covered by the execution policy")
        return self._with_writing_flags(executable, arguments, classification, cwd)

    def _with_writing_flags(
        self,
        executable: str,
        arguments: tuple[str, ...],
        classification: CommandClassification,
        cwd: str,
    ) -> CommandClassification:
        """Un comando que lee o verifica deja de hacerlo en cuanto lleva `--fix`.

        Escribir en el proyecto a traves de un proceso es escribir: pide la misma
        confirmacion explicita que cualquier cambio irreversible, no la autoridad de
        ejecutar tests.
        """
        if classification.tier in (RiskTier.R3_EXTERNAL_OR_IRREVERSIBLE, RiskTier.R4_FORBIDDEN):
            return classification
        writes = _WRITING_FLAGS.get(executable, frozenset())
        flag = next((item for item in arguments if _flag_name(item) in writes), None)
        rewrites = executable == "black" or (executable == "ruff" and "format" in arguments)
        if flag is None and rewrites and not any(item in _CHECK_ONLY_FLAGS for item in arguments):
            flag = "format"
        if flag is None:
            return classification
        return self._mutating(
            executable, cwd, f"{executable} {flag} rewrites files in the workspace"
        )

    def _classify_subcommand(
        self,
        executable: str,
        arguments: tuple[str, ...],
        subcommands: Mapping[str, str],
        cwd: str,
        workspace_root: Path | None,
        cwd_path: Path | None,
    ) -> CommandClassification:
        if executable == "git":
            forbidden = next((item for item in arguments if item in _GIT_GLOBAL_FORBIDDEN), None)
            if forbidden is not None:
                return self._forbidden(f"git {forbidden} changes what git reads or executes")
            positional = _git_positional(arguments)
        else:
            positional = [item for item in arguments if not item.startswith("-")]
        first = positional[0] if positional else None
        flag = next((item for item in arguments if item in subcommands), None)
        key = flag if flag in ("-m", "-c") else first
        if key is None:
            return self._read(executable, cwd)
        policy = subcommands.get(key)
        if policy is None:
            return self._forbidden(f"{executable} {key} is not covered by the execution policy")
        rest = tuple(positional[1:])
        if policy == "module":
            return self._classify_python_module(executable, arguments, cwd)
        if policy == "forbidden":
            return self._forbidden(f"{executable} {key} is forbidden")
        if policy == "read":
            return self._read(f"{executable} {key}", cwd)
        if policy == "build":
            return self._build(f"{executable} {key}", cwd)
        if policy == "script":
            script = rest[0] if rest else ""
            if script in _VERIFICATION_SCRIPTS:
                return self._build(f"{executable} {key} {script}", cwd)
            return CommandClassification(
                RiskTier.R3_EXTERNAL_OR_IRREVERSIBLE,
                RiskLevel.HIGH,
                "irreversible",
                f"{executable} {key} {script or '<script>'} runs a script the project defines, "
                "which may do anything (deploy, network, delete)",
                (
                    f"Runs the project script {script!r} in {cwd}",
                    "Executes arbitrary code from the project",
                ),
                concurrency_safe=False,
            )
        if policy == "wrapper":
            return self._classify_wrapped(executable, arguments, cwd, workspace_root, cwd_path)
        if policy == "git_branch":
            listing = not rest and all(item in _GIT_BRANCH_LISTING for item in _flags(arguments))
            if listing:
                return self._read("git branch", cwd)
            return self._mutating("git branch", cwd, "git branch creates, moves or deletes refs")
        if policy == "git_remote":
            if not rest or rest[0] in ("show", "get-url"):
                return self._read("git remote", cwd)
            return self._mutating("git remote", cwd, "git remote changes the configured remotes")
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

    def _classify_wrapped(
        self,
        executable: str,
        arguments: tuple[str, ...],
        cwd: str,
        workspace_root: Path | None,
        cwd_path: Path | None,
    ) -> CommandClassification:
        """`uv run X` es X: se clasifica lo que de verdad se ejecuta.

        Antes `uv run` era «build» sin mirar mas, asi que `uv run python -c ...` ejecutaba
        codigo arbitrario con la etiqueta de una compilacion (A02).
        """
        index = arguments.index("run") + 1
        while index < len(arguments) and arguments[index].startswith("-"):
            option = arguments[index]
            takes_value = option in _UV_RUN_OPTIONS_WITH_VALUE
            index += 2 if takes_value else 1
        wrapped = arguments[index:]
        if not wrapped:
            return self._forbidden(f"{executable} run needs a command")
        inner = self.classify(tuple(wrapped), cwd, workspace_root=workspace_root, cwd_path=cwd_path)
        if inner.tier is RiskTier.R2_LOCAL_EXECUTION and inner.category == "read":
            # Envuelto, un lector pasa por el entorno del proyecto: es ejecucion.
            return self._build(f"{executable} run {wrapped[0]}", cwd)
        return inner

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
                "Executes code the project defines (tests, build files, plugins)",
                "May write caches or build artefacts inside the workspace",
            ),
            concurrency_safe=False,
        )

    @staticmethod
    def _mutating(label: str, cwd: str, reason: str) -> CommandClassification:
        return CommandClassification(
            RiskTier.R3_EXTERNAL_OR_IRREVERSIBLE,
            RiskLevel.HIGH,
            "mutating",
            reason,
            (f"Runs {label} in {cwd}", "May change or delete files or repository state"),
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


#: Lo que `git branch` admite sin dejar de ser un listado.
_GIT_BRANCH_LISTING = frozenset(
    {
        "-a",
        "--all",
        "-r",
        "--remotes",
        "-l",
        "--list",
        "-v",
        "-vv",
        "--verbose",
        "--show-current",
        "--contains",
        "--no-contains",
        "--merged",
        "--no-merged",
        "--sort",
        "--format",
        "--points-at",
        "--color",
        "--no-color",
        "--column",
        "--no-column",
        "-i",
        "--ignore-case",
    }
)


def _flag_name(item: str) -> str:
    """`--output=x` es la opcion `--output`; `-o../x` tambien es `-o`."""
    if not item.startswith("-"):
        return ""
    if item.startswith("--"):
        return item.split("=", 1)[0]
    return item[:2]


def _flags(arguments: tuple[str, ...]) -> list[str]:
    return [_flag_name(item) for item in arguments if item.startswith("-")]


def _git_positional(arguments: tuple[str, ...]) -> list[str]:
    """Los argumentos sin opcion, saltando el valor de `-C <dir>`."""
    positional: list[str] = []
    skip = False
    for item in arguments:
        if skip:
            skip = False
            continue
        if item in _GIT_GLOBAL_WITH_VALUE:
            skip = True
            continue
        if not item.startswith("-"):
            positional.append(item)
    return positional


def _escaping_argument(arguments: tuple[str, ...], root: Path, cwd: Path) -> str | None:
    """El primer argumento que nombra una ruta fuera del workspace, si hay alguno.

    Se mira cada argumento y el valor de cada `--opcion=valor`. Solo se juzga lo que
    tiene forma de ruta (separadores, `..`, unidad o `~`): un argumento como `-q` o
    `print(1)` no nombra nada. Lo que tiene forma de ruta se resuelve con la misma
    canonicalizacion que las herramientas de ficheros, enlaces incluidos.
    """
    for item in arguments:
        candidates = [item]
        if item.startswith("-"):
            if "=" in item:
                candidates = [item.split("=", 1)[1]]
            elif len(item) > 2 and not item.startswith("--"):
                # `-o../x`: el valor pegado a una opcion corta.
                candidates = [item[2:]]
            else:
                continue
        for value in candidates:
            if not _looks_like_path(value):
                continue
            expanded = Path(value).expanduser()
            unresolved = expanded if expanded.is_absolute() else cwd / expanded
            try:
                canonical = unresolved.resolve(strict=False)
            except (OSError, RuntimeError):
                return value
            if not canonical.is_relative_to(root):
                return value
    return None


def _looks_like_path(value: str) -> bool:
    if not value or "://" in value:
        return False
    if value.startswith("~") or value == "..":
        return True
    if "/" in value or "\\" in value:
        return True
    return len(value) >= 2 and value[1] == ":" and value[0].isalpha()


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


def _kill_tree(process: asyncio.subprocess.Process) -> None:
    """Para registrar en una cancelacion, que espera un callback sin valor."""
    _terminate_tree(process)


def _terminate_tree(process: asyncio.subprocess.Process) -> bool:
    """Kill the child and everything it spawned, so no orphan survives a cancel.

    Job Object on Windows, process group elsewhere (see `process_tree`). Returns whether
    the kill was confirmed; `reap` is what proves the process is gone.
    """
    return terminate_tree(process)


async def _spawn_process(
    argv: tuple[str, ...], cwd: Path, env: Mapping[str, str] | None = None
) -> asyncio.subprocess.Process:
    """Start a child with pipes and no shell, isolated into its own group on POSIX."""
    environment = {**os.environ, **env} if env else None
    try:
        if sys.platform == "win32":
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                env=environment,
            )
            # En su job desde el primer momento: es lo que permite matar tambien a los
            # nietos cuando el padre ya no esta (A14).
            contain(process)
            return process
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
    unsubscribe = cancellation.register(partial(_kill_tree, process))
    try:
        raw_out, raw_err = await communicate_bounded(process, timeout_seconds)
    except (TimeoutError, asyncio.CancelledError):
        _terminate_tree(process)
        await reap(process)
        if cancellation.is_cancelled:
            raise ProcessCancelledError("Command cancelled and child terminated") from None
        raise ProcessTimeoutError(
            f"Command exceeded {timeout_seconds} seconds",
            details={"argv": list(argv)},
        ) from None
    finally:
        unsubscribe()
    release(process)
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
        # El techo entero, no un valor prudente. Un modelo que no dice cuanto puede tardar
        # su comando no esta pidiendo que se le corte pronto: simplemente no lo ha pensado,
        # y `pytest` en frio sobre un repositorio pequeño ya se pasa de treinta segundos.
        # Medido: con el default anterior, uno de cada seis runs de `qwen3.8:27b` moria por
        # esto —`process_timeout` sobre la suite— y el fallo no tenia nada que ver con el
        # trabajo pedido. Quien sepa que su comando debe ser corto lo dice en
        # `timeout_seconds`; quien no lo diga tiene el maximo.
        #
        # 600 y no 660: 660 es el techo del EJECUTOR (`ToolSpec.timeout_seconds`), que se
        # deja deliberadamente por encima para que sobre margen al arrancar y al matar el
        # arbol de procesos. Igualarlos haria que ganase el de fuera, y el fallo se leeria
        # como «la tool expiro» en vez de decir que comando se paso y de cuanto.
        default_timeout_seconds: float = 600.0,
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
        # Lo que un comando puede pedir como maximo, mas margen para arrancar y matar el
        # arbol de procesos. Sin esto, el techo generico del ejecutor cortaba a los 30 s
        # cualquier comando mas largo y lo contaba como que la tool habia expirado.
        timeout_seconds=660.0,
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
        return self.policy.classify(
            argv,
            context.workspace.relative(directory),
            workspace_root=context.workspace.root,
            cwd_path=directory,
        )

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
        unsubscribe = cancellation.register(partial(_kill_tree, process))
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
            raw_out, raw_err = await communicate_bounded(process, timeout)
        except (TimeoutError, asyncio.CancelledError):
            _terminate_tree(process)
            await reap(process)
            return "", "", True
        release(process)
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
