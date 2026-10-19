"""AI OS Core como director: autoriza cada run y recibe sus recibos.

Core es el plano de control del ecosistema del TFM. Athena no lo necesita: sin
`ATHENA_CORE_URL` todo sigue igual. Con él, `RunRegistry` pide permiso antes de crear
o reanudar un run (política `athena:broker`) y solo sigue con un ALLOW ejecutable; si
Core no contesta, el run no existe —quien configuró un director no quiere que se le
salte cuando se cae—. Al terminar, el run deja su recibo (`succeeded`, `failed_terminal`
o `cancelled`) para que Core sepa, tras una caída, si se puede reintentar.

Es un adaptador como el del broker: el runtime no sabe que existe Core.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from athena.errors import PermissionDeniedError

POLICY_SCOPE = "athena:broker"
#: Formato de flow_id que acepta el monitor de Core (cabecera `X-Flow-Id`).
_FLOW_ID = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")
#: Un salto para el monitor nunca debe hacer esperar al run.
_HOP_TIMEOUT_SECONDS = 3.0
#: Cómo se presenta Athena ante Core (debe coincidir con su token por app).
PRINCIPAL = "athena"


class CoreDeniedError(PermissionDeniedError):
    code = "core_denied"


class CoreUnavailableError(PermissionDeniedError):
    code = "core_unavailable"


class AiOsCoreGovernor:
    def __init__(
        self, base_url: str, token: str | None = None, *, timeout_seconds: float = 10.0
    ) -> None:
        parsed = urlparse(base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("ATHENA_CORE_URL debe ser una URL http o https")
        self._base_url = base_url.strip().rstrip("/")
        self._token = token.strip() if token else None
        self._timeout_seconds = timeout_seconds

    async def authorize(self, run_id: str, context: dict[str, Any]) -> None:
        """Lanza si Core no da un ALLOW ejecutable: entonces no se manda nada al modelo."""
        try:
            decision = await asyncio.to_thread(
                self._post,
                "/api/v1/policies/evaluate",
                {
                    "scope": POLICY_SCOPE,
                    "principal_id": PRINCIPAL,
                    "resource_id": run_id,
                    "context": context,
                },
            )
        except _CoreHttpError as error:
            raise CoreUnavailableError(f"{error}. Por seguridad el run no se ha creado.") from None
        if decision.get("authorization") != "ALLOW" or decision.get("executable") is not True:
            raise CoreDeniedError(_denied_text(decision), details={"core_decision": decision})

    async def hop(
        self,
        flow_id: str,
        kind: str,
        summary: str = "",
        details: dict[str, Any] | None = None,
        target: str = "ai_broker",
    ) -> None:
        """Cuenta al monitor de Core una llamada de Athena a otra pieza. Enviar y olvidar."""
        with contextlib.suppress(Exception):  # el monitor es secundario: jamás tumba un run
            await asyncio.to_thread(
                self._post,
                "/api/v1/flows/hops",
                {
                    "flow_id": flow_id,
                    "source_system": PRINCIPAL,
                    "target_system": target,
                    "kind": kind,
                    "summary": summary[:200],
                    "details": details or {},
                },
                _HOP_TIMEOUT_SECONDS,
            )

    async def receipt(self, run_id: str, status: str, evidence: dict[str, Any]) -> None:
        """Un recibo perdido no tumba el run: Core verá el siguiente estado."""
        with contextlib.suppress(CoreUnavailableError, _CoreHttpError):
            await asyncio.to_thread(
                self._post,
                "/api/v1/operations/receipts",
                {
                    "operation_id": run_id,
                    "receiver_service": "athena",
                    "status": status,
                    "evidence": evidence,
                },
            )

    def _post(
        self, path: str, payload: dict[str, Any], timeout: float | None = None
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Principal-Id": PRINCIPAL,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = Request(
            self._base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout or self._timeout_seconds) as response:
                result: dict[str, Any] = json.loads(response.read().decode("utf-8") or "{}")
                return result
        except HTTPError as exc:
            raise _CoreHttpError(exc.code, exc.read().decode("utf-8", "replace")[:300]) from None
        except (URLError, TimeoutError, OSError) as exc:
            raise CoreUnavailableError(
                "No se pudo consultar a AI OS Core. Por seguridad el run no se ha creado."
            ) from exc


class _CoreHttpError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"AI OS Core respondió {status}: {detail}")
        self.status = status


def _denied_text(decision: dict[str, Any]) -> str:
    authorization = decision.get("authorization")
    if authorization == "REQUIRE_APPROVAL":
        return (
            "AI OS Core pide una aprobación humana antes de este run "
            f"(políticas de «{POLICY_SCOPE}»)."
        )
    if authorization == "INDETERMINATE":
        missing = ", ".join(decision.get("unknown_inputs") or []) or "algún dato"
        return (
            f"AI OS Core no puede decidir (le falta: {missing}); por seguridad el run no se crea."
        )
    return (
        f"AI OS Core no autoriza este run ({authorization}). "
        f"Revisa las políticas de «{POLICY_SCOPE}»."
    )


def valid_flow_id(value: str | None) -> str | None:
    """El flow_id que llega de otra app si tiene un formato que Core acepte; si no, ninguno."""
    value = (value or "").strip()
    return value if _FLOW_ID.match(value) else None


def governor_from_environment() -> AiOsCoreGovernor | None:
    """`ATHENA_CORE_URL` (y su token `ATHENA_CORE_TOKEN`), o `None`: sin Core todo sigue igual."""
    url = os.environ.get("ATHENA_CORE_URL", "").strip()
    if not url:
        return None
    return AiOsCoreGovernor(url, os.environ.get("ATHENA_CORE_TOKEN", "").strip() or None)
