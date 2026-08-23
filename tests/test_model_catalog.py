"""Elegir modelo por run: que se ofrece, que se rechaza y que llega al proveedor.

El ultimo test es el que importa mas y es el que este proyecto se salta una y otra vez: no
basta con que el catalogo exista y valide bien, tiene que llegar hasta la peticion que sale
hacia el proveedor. `ModelRequest.model` llevaba desde siempre en el contrato y el adaptador
del broker ya lo miraba; lo que faltaba era que alguien lo rellenase.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from athena.adapters.service.runs import RunOptions
from athena.agent_loop import AgentLoop, AgentLoopConfig, AgentRunStatus
from athena.cancellation import CancellationSource
from athena.context import ContextBuilder
from athena.errors import ToolValidationError
from athena.events import InMemoryEventBus
from athena.model_catalog import ModelCatalog
from athena.models import ModelResponse
from athena.permissions import ReadOnlyPermissionEngine
from athena.registry import ToolRegistry
from athena.repository_tools import repository_read_tools
from athena.stores import InMemoryToolResultStore
from athena.testing import FakeModelProvider
from athena.tool_executor import ToolExecutor
from athena.workspace import Workspace


def test_the_offered_order_is_the_one_the_deployment_wrote() -> None:
    """No alfabetico: quien despliega pone primero lo que quiere que se use."""
    catalog = ModelCatalog(("qwen3.8:27b", "DeepSeek-V4-Pro", "granite4.1:30b"))

    assert catalog.names() == ("qwen3.8:27b", "DeepSeek-V4-Pro", "granite4.1:30b")
    assert catalog.default == "qwen3.8:27b"


def test_a_repeated_name_is_offered_once() -> None:
    assert ModelCatalog(("a", "b", "a", " b ")).names() == ("a", "b")


def test_the_default_is_always_offered() -> None:
    """Tenerlo activo y fuera de la lista describiria mal el despliegue."""
    catalog = ModelCatalog(("a", "b"), default="c")

    assert catalog.default == "c"
    assert catalog.names() == ("c", "a", "b")


def test_an_empty_catalog_is_refused() -> None:
    with pytest.raises(ValueError):
        ModelCatalog(())


def test_asking_for_nothing_gets_the_default() -> None:
    """Es lo que manda cualquier cliente sin selector, asi que no puede ser un error."""
    catalog = ModelCatalog(("a", "b"))

    assert catalog.resolve("") == "a"
    assert catalog.resolve(None) == "a"


def test_an_unknown_model_is_refused_and_the_options_are_named() -> None:
    catalog = ModelCatalog(("a", "b"))

    with pytest.raises(ToolValidationError) as caught:
        catalog.resolve("c")

    assert "c" in caught.value.message
    assert "a, b" in caught.value.message


def test_the_run_options_carry_the_chosen_model() -> None:
    assert RunOptions.from_json({"model": "  DeepSeek-V4-Pro "}).model == "DeepSeek-V4-Pro"
    assert RunOptions.from_json({}).model == ""


def test_the_chosen_model_reaches_the_provider(tmp_path: Path) -> None:
    """La costura entera, de la opcion del run a la peticion que sale.

    Sin este test el catalogo seria otro subsistema completo, probado y conectado a nada:
    validaria nombres preciosos que nadie llegaria a usar.
    """
    (tmp_path / "README.md").write_text("# Repo\n", encoding="utf-8")

    async def scenario() -> None:
        provider = FakeModelProvider([ModelResponse("Listo.", "fake", "stop")])
        workspace = Workspace.from_path(tmp_path, "test-workspace")
        registry = ToolRegistry(repository_read_tools())
        bus = InMemoryEventBus()
        executor = ToolExecutor(
            registry, ReadOnlyPermissionEngine(), InMemoryToolResultStore(), bus
        )
        loop = AgentLoop(
            provider,
            registry,
            executor,
            ContextBuilder(workspace),
            bus,
            config=AgentLoopConfig(retry_backoff_seconds=0, model="DeepSeek-V4-Pro"),
        )

        result = await loop.run("Say hello", workspace, CancellationSource().token)

        assert result.status is AgentRunStatus.COMPLETED
        assert provider.requests[-1].model == "DeepSeek-V4-Pro"

    asyncio.run(scenario())


def test_without_a_choice_the_request_names_no_model(tmp_path: Path) -> None:
    """La conducta anterior sobrevive: el proveedor aplica lo que tenga configurado."""
    (tmp_path / "README.md").write_text("# Repo\n", encoding="utf-8")

    async def scenario() -> None:
        provider = FakeModelProvider([ModelResponse("Listo.", "fake", "stop")])
        workspace = Workspace.from_path(tmp_path, "test-workspace")
        registry = ToolRegistry(repository_read_tools())
        bus = InMemoryEventBus()
        executor = ToolExecutor(
            registry, ReadOnlyPermissionEngine(), InMemoryToolResultStore(), bus
        )
        loop = AgentLoop(
            provider,
            registry,
            executor,
            ContextBuilder(workspace),
            bus,
            config=AgentLoopConfig(retry_backoff_seconds=0),
        )

        await loop.run("Say hello", workspace, CancellationSource().token)

        assert provider.requests[-1].model is None

    asyncio.run(scenario())
