"""Politica de artefactos: comprueba que existe lo que se dijo que se crearia."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from athena.cancellation import CancellationToken
from athena.errors import WorkspaceBoundaryError
from athena.state import SessionState
from athena.verification.contratos import (
    VerificationEvidence,
    VerificationResult,
    VerificationStatus,
)
from athena.workspace import Workspace


class ArtifactVerificationPolicy:
    """Evidencia para un dominio donde no hay comandos que ejecutar.

    No todo trabajo se comprueba corriendo algo. Un encargo cuyo resultado es un documento
    no tiene suite que pase ni compilador que se queje, y hasta ahora eso significaba que
    Athena no podia terminar nunca: sin plan de verificacion la unica salida era
    «inconclusive», que tras ADR-027 es exactamente lo que hay que decir cuando no se pudo
    comprobar nada. Correcto, y ademas inutil como unico final posible.

    Asi que aqui la evidencia es otra: **los entregables existen, no estan vacios y este
    run los toco**. Es deterministico, lo produce el runtime y no depende de que el modelo
    diga que ha terminado, que es lo que exige el contrato de verificacion.

    Lo que NO demuestra hay que decirlo igual de claro, porque un verde que se lee como
    mas de lo que es hace mas daño que un rojo: esto prueba que algo se produjo, no que
    sea bueno. Ningun automatismo puede juzgar si un documento cumple su encargo, y fingir
    que si convertiria la verificacion en un sello.
    """

    #: Lo que esta politica afirma cuando pasa, para que viaje con la evidencia.
    PROVES = (
        "The declared deliverables exist, are non-empty and were written by this run. "
        "It does not establish that their content is correct."
    )

    def __init__(self, expected: Sequence[str] = ()) -> None:
        #: Entregables pedidos por quien encargo el trabajo. Sin ellos se comprueba lo
        #: que el run declara haber escrito, que es mas debil y se reporta como tal.
        self.expected = tuple(expected)

    async def verify(
        self, state: SessionState, workspace: Workspace, cancellation: CancellationToken
    ) -> VerificationResult:
        cancellation.raise_if_cancelled()
        written = _declared_paths(state)
        targets = self.expected or written
        if not targets:
            # Ni se pidio nada concreto ni el run escribio nada. No hay con que
            # demostrar ni exito ni fracaso, y decir cualquiera de los dos seria
            # inventarselo.
            return VerificationResult(
                VerificationStatus.INCONCLUSIVE,
                (
                    VerificationEvidence(
                        kind="artifact",
                        summary=(
                            "Nothing was produced and no deliverable was declared, so "
                            "there is nothing to show either way."
                        ),
                        metadata={"expected": [], "written": []},
                    ),
                ),
                "Verification is inconclusive: no deliverable was produced or declared.",
            )
        # Rutas canonicas en los dos lados: `./report.txt` y `report.txt` son el mismo
        # fichero, y compararlas como texto hacia que un entregable escrito de verdad
        # pareciera ajeno.
        touched_paths = {path for path in (_canonical(workspace, i) for i in written) if path}
        evidence: list[VerificationEvidence] = []
        problems: list[str] = []
        for relative in targets:
            cancellation.raise_if_cancelled()
            try:
                path = workspace.resolve(relative, must_exist=False)
            except WorkspaceBoundaryError:
                problems.append(f"{relative} is outside the workspace")
                continue
            exists = path.is_file()
            size = path.stat().st_size if exists else 0
            touched = path in touched_paths
            passed = exists and size > 0 and (touched or not self.expected)
            # Tres negativos distintos que piden cosas distintas a quien lo lea.
            if not exists:
                problems.append(f"{relative} does not exist")
            elif size == 0:
                problems.append(f"{relative} is empty")
            elif not passed:
                problems.append(f"{relative} exists but was not written by this run")
            evidence.append(
                VerificationEvidence(
                    kind="artifact",
                    summary=f"{relative}: {'produced' if passed else 'not produced'}",
                    reference=relative,
                    metadata={
                        "name": relative,
                        "passed": passed,
                        "exists": exists,
                        "size_bytes": size,
                        "written_by_this_run": touched,
                    },
                )
            )
        if problems:
            return VerificationResult(
                VerificationStatus.FAILED,
                tuple(evidence),
                "Verification failed: " + "; ".join(problems) + ".",
            )
        return VerificationResult(
            VerificationStatus.PASSED,
            tuple(evidence),
            f"{len(evidence)} deliverable(s) produced. {self.PROVES}",
        )


class AnswerVerificationPolicy:
    """La evidencia de una consulta: hay respuesta y no se toco nada.

    Existe para no tener que elegir entre dos mentiras. Tratar una consulta como trabajo
    de software la hace fallar siempre que no haya tests que correr (o correrlos para
    nada); tratarla como «el modelo termino» la hacia pasar tambien cuando se habia pedido
    cambiar un fichero y solo llego texto (A04). Aqui pasa si hubo respuesta y ningun
    fichero cambio, y el resultado dice que eso es todo lo que demuestra.
    """

    PROVES = (
        "The run answered without changing any file. It does not establish that the "
        "answer is correct."
    )

    async def verify(
        self, state: SessionState, workspace: Workspace, cancellation: CancellationToken
    ) -> VerificationResult:
        del workspace
        cancellation.raise_if_cancelled()
        answer = state.attributes.get("final_response")
        written = _declared_paths(state)
        if not isinstance(answer, str) or not answer.strip():
            return VerificationResult(
                VerificationStatus.FAILED,
                (VerificationEvidence(kind="answer", summary="No answer was given."),),
                "Verification failed: the run gave no answer.",
            )
        if written:
            return VerificationResult(
                VerificationStatus.FAILED,
                (
                    VerificationEvidence(
                        kind="answer",
                        summary="A question run changed files.",
                        metadata={"written": list(written)},
                    ),
                ),
                "Verification failed: a question run must not change files, and this one "
                "changed " + ", ".join(written) + ".",
            )
        return VerificationResult(
            VerificationStatus.PASSED,
            (
                VerificationEvidence(
                    kind="answer",
                    summary="Answered without changing any file.",
                    metadata={"answer_chars": len(answer), "passed": True},
                ),
            ),
            f"Answer given; no file was changed. {self.PROVES}",
        )


def _canonical(workspace: Workspace, relative: str) -> Path | None:
    try:
        return workspace.resolve(relative, must_exist=False)
    except WorkspaceBoundaryError:
        return None


def _declared_paths(state: SessionState) -> tuple[str, ...]:
    raw = state.attributes.get("files_modified")
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str) and item)
