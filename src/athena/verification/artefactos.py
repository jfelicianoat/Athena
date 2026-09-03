"""Politica de artefactos: comprueba que existe lo que se dijo que se crearia."""

from __future__ import annotations

from collections.abc import Sequence

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
        evidence: list[VerificationEvidence] = []
        missing: list[str] = []
        for relative in targets:
            cancellation.raise_if_cancelled()
            try:
                path = workspace.resolve(relative, must_exist=False)
            except WorkspaceBoundaryError:
                missing.append(relative)
                continue
            exists = path.is_file()
            size = path.stat().st_size if exists else 0
            touched = relative in written
            passed = exists and size > 0 and (touched or not self.expected)
            if not passed:
                missing.append(relative)
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
        if missing:
            return VerificationResult(
                VerificationStatus.FAILED,
                tuple(evidence),
                "Verification failed: "
                + ", ".join(sorted(missing))
                + " was not produced as a non-empty file.",
            )
        return VerificationResult(
            VerificationStatus.PASSED,
            tuple(evidence),
            f"{len(evidence)} deliverable(s) produced. {self.PROVES}",
        )


def _declared_paths(state: SessionState) -> tuple[str, ...]:
    raw = state.attributes.get("files_modified")
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str) and item)
