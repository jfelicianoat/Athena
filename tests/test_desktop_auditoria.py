"""El escritorio frente a la auditoria del 28-sep-2026: A01, A03, A04, A15, A17 y A18.

Se prueba por el camino que usa la ventana (`run_athena` sobre `RunRegistry`) con un
proveedor simulado, y la ventana misma con un Tk real cuando el sistema lo tiene.
"""

from __future__ import annotations

import asyncio
import tkinter as tk
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from athena.agent_loop import AgentRunStatus
from athena.cancellation import CancellationSource
from athena.models import ModelResponse, ModelToolCall
from athena.permissions import PermissionDecision, PermissionRequest
from athena.testing import FakeModelProvider
from athena_desktop.config import ProviderKind, SettingsStore
from athena_desktop.presentacion import present_approval, present_result
from athena_desktop.runtime import RunConfiguration, list_runs, roll_back_run, run_athena

_MARKER_TEST = (
    "import unittest\n"
    "from pathlib import Path\n\n\n"
    "class Marker(unittest.TestCase):\n"
    "    def test_marker(self):\n"
    "        p = Path('verification_marker.txt')\n"
    "        p.write_text((p.read_text() if p.exists() else '') + 'executed\\n')\n"
)


def _configuration(root: Path, **overrides: object) -> RunConfiguration:
    values: dict[str, object] = {
        "workspace": root,
        "objective": "Describe el proyecto",
        "provider": ProviderKind.OPENAI_COMPATIBLE,
        "base_url": "http://127.0.0.1:1/v1",
        "model": "fake",
    }
    values.update(overrides)
    return RunConfiguration(**values)  # type: ignore[arg-type]


def _run(
    configuration: RunConfiguration,
    provider: FakeModelProvider,
    state: Path,
    decide: PermissionDecision = PermissionDecision.DENY,
) -> tuple[object, list[PermissionRequest]]:
    asked: list[PermissionRequest] = []

    def on_permission(request: PermissionRequest) -> PermissionDecision:
        asked.append(request)
        return decide

    with patch("athena_desktop.runtime.build_provider", return_value=provider):
        result = asyncio.run(
            run_athena(
                configuration,
                CancellationSource(),
                on_event=lambda event: None,
                on_permission=on_permission,
                state_dir=state,
            )
        )
    return result, asked


def test_a01_desktop_with_execution_off_runs_nothing_of_the_project(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "AGENTS.md").write_text(
        "## Verification\n\n```text\npython -m unittest -q\n```\n", encoding="utf-8"
    )
    (root / "test_marker.py").write_text(_MARKER_TEST, encoding="utf-8")
    provider = FakeModelProvider(
        [
            ModelResponse(
                "",
                "fake",
                "tool_calls",
                tool_calls=(ModelToolCall("w", "write_file", {"path": "a.txt", "content": "x"}),),
            ),
            ModelResponse("Hecho.", "fake", "stop"),
        ]
    )
    configuration = _configuration(
        root, objective="Crea a.txt", task_kind="change", writes="allow", execution="off"
    )
    result, asked = _run(configuration, provider, tmp_path / "state")
    assert not (root / "verification_marker.txt").exists()
    assert asked == []
    assert result.status is not AgentRunStatus.COMPLETED  # type: ignore[attr-defined]


def test_a04_asking_for_a_file_and_getting_only_text_is_not_success(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    provider = FakeModelProvider([ModelResponse("He corregido main.py.", "fake", "stop")] * 20)
    configuration = _configuration(
        root, objective="Corrige main.py", task_kind="change", writes="allow", max_iterations=3
    )
    result, _ = _run(configuration, provider, tmp_path / "state")
    assert result.status is not AgentRunStatus.COMPLETED  # type: ignore[attr-defined]
    assert not (root / "main.py").exists()


def test_a04_a_question_ends_as_an_answer_and_says_so(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    provider = FakeModelProvider([ModelResponse("Es una calculadora.", "fake", "stop")])
    result, _ = _run(_configuration(root), provider, tmp_path / "state")
    assert result.status is AgentRunStatus.COMPLETED  # type: ignore[attr-defined]
    view = present_result(result, task_kind="question")  # type: ignore[arg-type]
    assert "no se ha cambiado ningún archivo" in view.headline
    assert "no demuestra" in view.explanation


def test_a18_an_approval_shows_what_a_write_would_change(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    provider = FakeModelProvider(
        [
            ModelResponse(
                "",
                "fake",
                "tool_calls",
                tool_calls=(
                    ModelToolCall(
                        "e",
                        "edit_file",
                        {"path": "calc.py", "old_string": "a - b", "new_string": "a + b"},
                    ),
                ),
            ),
            ModelResponse("Arreglado.", "fake", "stop"),
        ]
    )
    configuration = _configuration(
        root, objective="Arregla la suma", task_kind="change", writes="ask"
    )
    result, asked = _run(configuration, provider, tmp_path / "state", PermissionDecision.DENY)
    assert [request.tool_name for request in asked] == ["edit_file"]
    view = present_approval(asked[0])
    assert "-    return a - b" in view.preview
    assert "+    return a + b" in view.preview
    # Denegado: el archivo sigue como estaba.
    assert "a - b" in (root / "calc.py").read_text(encoding="utf-8")
    del result


def test_desktop_history_and_undo_work_after_the_window_closed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    state = tmp_path / "state"
    provider = FakeModelProvider(
        [
            ModelResponse(
                "",
                "fake",
                "tool_calls",
                tool_calls=(
                    ModelToolCall("w", "write_file", {"path": "nuevo.md", "content": "hola"}),
                ),
            ),
            ModelResponse("Escrito.", "fake", "stop"),
        ]
    )
    configuration = _configuration(
        root,
        objective="Escribe nuevo.md",
        task_kind="documents",
        writes="allow",
        deliverables=("nuevo.md",),
    )
    result, _ = _run(configuration, provider, state)
    assert result.status is AgentRunStatus.COMPLETED  # type: ignore[attr-defined]
    runs = asyncio.run(list_runs(state))
    assert len(runs) == 1
    assert runs[0].undoable == 1
    rolled = asyncio.run(roll_back_run(runs[0].run_id, state))
    assert rolled.restored == ("nuevo.md",)
    assert not (root / "nuevo.md").exists()


def test_a17_an_empty_or_relative_project_is_refused() -> None:
    with pytest.raises(ValueError, match="carpeta del proyecto"):
        _configuration(Path("")).validate()
    with pytest.raises(ValueError, match="carpeta del proyecto"):
        _configuration(Path("relativa")).validate()


# ------------------------------------------------------------------ la ventana


@pytest.fixture(scope="module")
def tk_root() -> Iterator[tk.Tk]:
    """Un solo interprete Tk por modulo: Tcl en Windows no soporta crear varios seguidos
    en el mismo proceso («invalid command name tcl_findLibrary»)."""
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk no esta disponible: {exc}")
    root.withdraw()
    try:
        yield root
    finally:
        root.destroy()


@pytest.fixture
def window(tk_root: tk.Tk, tmp_path: Path) -> Iterator[object]:
    from athena_desktop.app import AthenaDesktopApp

    top = tk.Toplevel(tk_root)
    top.withdraw()
    app = AthenaDesktopApp(
        top,  # type: ignore[arg-type]
        SettingsStore(tmp_path / "settings.json"),
        state_dir=tmp_path / "state",
    )
    try:
        yield app
    finally:
        app.closing = True
        top.destroy()


def test_a03_switching_provider_never_carries_the_token_across(window: object) -> None:
    app = window
    app.token.set("TOKEN-DEL-BROKER")  # type: ignore[attr-defined]
    app.provider.set("OpenAI compatible")  # type: ignore[attr-defined]
    app._provider_changed()  # type: ignore[attr-defined]
    assert app.token.get() != "TOKEN-DEL-BROKER"  # type: ignore[attr-defined]
    assert app.base_url.get() == "http://localhost:1234/v1"  # type: ignore[attr-defined]
    # Volver al broker recupera su token y su URL, no los del otro.
    app.provider.set("AI_Broker")  # type: ignore[attr-defined]
    app._provider_changed()  # type: ignore[attr-defined]
    assert app.token.get() == "TOKEN-DEL-BROKER"  # type: ignore[attr-defined]


def test_a03_changing_the_url_does_not_reuse_the_token(window: object) -> None:
    app = window
    app.token.set("TOKEN-A")  # type: ignore[attr-defined]
    app.base_url.set("http://otro-broker:8765")  # type: ignore[attr-defined]
    app._switch_credential()  # type: ignore[attr-defined]
    assert app.token.get() != "TOKEN-A"  # type: ignore[attr-defined]


def test_a17_the_window_will_not_start_without_a_project(window: object) -> None:
    app = window
    app.workspace.set("")  # type: ignore[attr-defined]
    app.objective.insert("1.0", "Describe el proyecto")  # type: ignore[attr-defined]
    app._refresh()  # type: ignore[attr-defined]
    assert str(app.run_button.cget("state")) == tk.DISABLED  # type: ignore[attr-defined]
    assert "Elige la carpeta" in app.project_info.get()  # type: ignore[attr-defined]


def test_a15_a_service_that_dies_is_shown_as_stopped(window: object) -> None:
    from athena_desktop.service import ServiceState

    class _DeadService:
        state = ServiceState.FAILED

        class process:
            returncode = 3

    app = window
    app.managed_service = _DeadService()  # type: ignore[attr-defined]
    app.service_url.set("http://127.0.0.1:8770")  # type: ignore[attr-defined]
    app._watch_service()  # type: ignore[attr-defined]
    assert app.managed_service is None  # type: ignore[attr-defined]
    assert app.service_url.get() == ""  # type: ignore[attr-defined]
    assert "inesperadamente" in app.service_status.get()  # type: ignore[attr-defined]
