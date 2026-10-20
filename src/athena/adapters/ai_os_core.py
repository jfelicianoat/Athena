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
from pathlib import Path
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


def core_connection(principal: str = PRINCIPAL) -> tuple[str, str] | None:
    """URL y token de esta app en Core, tal como Core los deja en su carpeta de conexión.

    Core genera un token por app y lo escribe en %LOCALAPPDATA%/AIOSCore/conexion: así
    no hay que pegar ninguno. Sin Core instalado (o sin haber arrancado nunca), None.
    """
    local = os.environ.get("LOCALAPPDATA")
    folder = os.environ.get("AI_OS_CONNECT_DIR") or (str(Path(local, "AIOSCore", "conexion")) if local else "")
    if not folder:
        return None
    try:
        data = json.loads(Path(folder, f"{principal}.json").read_text(encoding="utf-8"))
        url, token = str(data["core_url"]), str(data["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return (url, token) if url and token else None


def core_settings() -> tuple[str, str | None] | None:
    """`ATHENA_CORE_URL`/`ATHENA_CORE_TOKEN` y, si faltan, los que deja Core al arrancar."""
    url = os.environ.get("ATHENA_CORE_URL", "").strip()
    token = os.environ.get("ATHENA_CORE_TOKEN", "").strip() or None
    found = core_connection()
    if found and (not url or url.rstrip("/") == found[0].rstrip("/")):
        url, token = url or found[0], token or found[1]
    return (url, token) if url else None


class CoreBrokerToken:
    """El token del AI Broker, pedido a Core: el Broker lo rota en cada arranque.

    Se llama en cada petición; `refresh=True` (tras un 401/403 del Broker) lo vuelve a
    pedir. Si Core no lo tiene, se usa `fallback` (el token del entorno, si lo hay).
    """

    def __init__(self, core_url: str, core_token: str, fallback: str | None = None, timeout: float = 5.0) -> None:
        self._url = core_url.rstrip("/") + "/api/v1/credentials/broker"
        self._core_token = core_token
        self._fallback = fallback
        self._timeout = timeout
        self._token: str | None = None

    def __call__(self, refresh: bool = False) -> str | None:
        if refresh or self._token is None:
            self._token = self._fetch() or self._token
        return self._token or self._fallback

    def _fetch(self) -> str | None:
        request = Request(self._url, headers={
            "Authorization": f"Bearer {self._core_token}", "Accept": "application/json"})
        try:
            with urlopen(request, timeout=self._timeout) as response:  # noqa: S310 - URL de Core
                token = json.load(response).get("token")
        except (OSError, ValueError, AttributeError):
            return None
        return token if isinstance(token, str) and token else None


def broker_token_source(fallback: str | None = None) -> CoreBrokerToken | None:
    """Con Core, el token del Broker lo da Core (nadie lo pega); `fallback` queda de reserva."""
    core = core_settings()
    if core is None or not core[1]:
        return None
    return CoreBrokerToken(core[0], core[1], fallback or None)


def governor_from_environment() -> AiOsCoreGovernor | None:
    """Core del entorno o de su carpeta de conexión, o `None`: sin Core todo sigue igual."""
    core = core_settings()
    if core is None:
        return None
    return AiOsCoreGovernor(core[0], core[1])
