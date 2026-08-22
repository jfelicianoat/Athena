"""Para qué se está usando Athena, y por tanto qué cuenta como prueba de haberlo hecho.

Un `AthenaProfile` **no es** un `SubagentProfile`, y confundirlos sería fácil porque
comparten la palabra. ADR-015 define tres perfiles de *rol* —Explorer, Coder, Verifier—
que reparten autoridad **dentro** de un run. Esto es la capa de encima: qué clase de
trabajo es el run entero. Un run con perfil de documentos sigue pudiendo delegar en un
Coder; lo que cambia es en qué consiste el trabajo, con qué herramientas se hace y qué
hay que enseñar para darlo por terminado.

## Por qué hacía falta

Athena se escribió para repositorios de software y lo decía en todas partes: el prompt
empieza con «a coding agent working in a repository», la verificación descubre comandos en
`pyproject.toml` y `package.json`, y desde ADR-027 un dominio sin comandos ejecutables
termina siempre en «no se pudo verificar». Correcto como diagnóstico y, como único final
posible, una forma educada de decir que Athena sólo sirve para código.

Un perfil declara las cuatro cosas que de verdad cambian entre dominios:

1. **de qué está hecho el sitio** donde se trabaja, que es la palabra que va al prompt;
2. **qué herramientas existen** — existir, no estar permitidas: lo que no está en el
   catálogo del perfil no se puede pedir, igual que hace `registry_for()` con los roles;
3. **qué cuenta como evidencia**, comandos ejecutados o entregables producidos;
4. **qué demuestra esa evidencia**, dicho en voz alta, incluida la parte que no demuestra.

Ese cuarto punto es el que impide que esto se convierta en una forma de aprobar runs sin
comprobarlos. Un perfil puede declarar que su evidencia es más débil; lo que no puede es
callárselo.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from athena.errors import ToolValidationError
from athena.types import JSONObject


class Evidence(StrEnum):
    """De dónde sale la prueba de que el trabajo está hecho."""

    #: Los checks del propio proyecto se ejecutan y pasan. La prueba más fuerte que hay.
    EXECUTED_CHECKS = "executed_checks"
    #: Los entregables existen, no están vacíos y los escribió este run. Prueba que algo
    #: se produjo, no que sea bueno.
    PRODUCED_ARTIFACTS = "produced_artifacts"


@dataclass(frozen=True, slots=True)
class AthenaProfile:
    """Para qué se usa Athena en este run."""

    name: str
    #: El sitio donde se trabaja, en las palabras del dominio. Va literal al prompt: a un
    #: run sobre documentos llamarle repositorio le enseña un vocabulario que no es suyo
    #: y lo empuja a buscar cosas que ahi no hay.
    subject: str
    evidence: Evidence
    #: Qué demuestra su evidencia, y qué no. Viaja con el resultado.
    proves: str
    #: Las tools que existen en este perfil, por nombre.
    tools: tuple[str, ...]
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name or not self.subject:
            raise ValueError("Un perfil necesita nombre y sujeto")
        if not self.tools:
            raise ValueError(f"El perfil {self.name} no deja usar ninguna herramienta")

    def catalog_from(self, available: Mapping[str, object]) -> dict[str, object]:
        """Las tools de este perfil, tomadas de lo que el despliegue tenga montado.

        Falla si falta alguna en vez de seguir con menos. Un perfil que se conforma con
        las que haya se convierte en otro perfil sin decirlo, y el run se comportaría
        distinto segun la maquina donde corriese.
        """
        missing = sorted(name for name in self.tools if name not in available)
        if missing:
            raise ToolValidationError(
                f"El perfil {self.name} necesita herramientas que no existen aqui: "
                f"{', '.join(missing)}"
            )
        return {name: available[name] for name in self.tools}

    def to_json(self) -> JSONObject:
        return {
            "name": self.name,
            "subject": self.subject,
            "evidence": self.evidence.value,
            "proves": self.proves,
            "tools": list(self.tools),
            "description": self.description,
        }


#: Herramientas de lectura, comunes a cualquier dominio: mirar lo que hay no depende de
#: qué sea lo que hay.
_READING = ("glob", "grep", "read_file", "read_range", "list_directory")
_WRITING = ("write_file", "edit_file")

SOFTWARE_ENGINEERING = AthenaProfile(
    name="software_engineering",
    subject="a repository",
    evidence=Evidence.EXECUTED_CHECKS,
    proves=(
        "The project's own checks were executed and passed, and the change did not "
        "weaken what does the verifying."
    ),
    tools=(
        *_READING,
        *_WRITING,
        "bash",
        "git_status",
        "git_diff",
        "git_log",
        "git_show",
        "git_commit",
    ),
    description="El comportamiento de siempre: codigo, comandos propios del proyecto y git.",
)

DOCUMENTS = AthenaProfile(
    name="documents",
    subject="a collection of documents",
    evidence=Evidence.PRODUCED_ARTIFACTS,
    proves=(
        "The declared deliverables exist, are non-empty and were written by this run. "
        "It does not establish that their content is correct."
    ),
    # Ni shell ni git, y no por prudencia: es lo que prueba que el nucleo no los
    # necesitaba. Un perfil no-desarrollador que se quedase `bash` no demostraria nada.
    tools=(*_READING, *_WRITING),
    description=(
        "Trabajo cuyo resultado es texto: no hay suite que pase ni compilador que se queje."
    ),
)


class ProfileRegistry:
    """Los perfiles que este despliegue ofrece."""

    def __init__(self, profiles: Iterable[AthenaProfile] = (), *, default: str = "") -> None:
        entries = tuple(profiles) or (SOFTWARE_ENGINEERING, DOCUMENTS)
        self._profiles = {profile.name: profile for profile in entries}
        if len(self._profiles) != len(entries):
            raise ValueError("Dos perfiles con el mismo nombre")
        self._default = default or entries[0].name
        if self._default not in self._profiles:
            raise ValueError(f"El perfil por defecto {self._default} no esta registrado")

    @property
    def default(self) -> AthenaProfile:
        return self._profiles[self._default]

    def get(self, name: str | None) -> AthenaProfile:
        """El perfil pedido, o el de por defecto si no se pidio ninguno.

        Un nombre desconocido es un error y no un motivo para caer al de por defecto:
        quien pide `documents` y recibe el de software no se entera hasta que Athena
        intenta ejecutar los tests de una carpeta de textos.
        """
        if not name:
            return self.default
        try:
            return self._profiles[name]
        except KeyError:
            raise ToolValidationError(
                f"Perfil desconocido: {name}. Disponibles: {', '.join(sorted(self._profiles))}"
            ) from None

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._profiles))


__all__ = [
    "DOCUMENTS",
    "SOFTWARE_ENGINEERING",
    "AthenaProfile",
    "Evidence",
    "ProfileRegistry",
]
