"""Arrancar Telegram **dentro** del servicio de Athena.

El paquete traía el cableado escrito en su docstring y ningún punto de entrada, así que
para usarlo había que copiar el fragmento en un script propio. Es la forma educada de tener
un subsistema que no corre: existe, está probado, y no lo arranca nadie.

## Por qué dentro y no aparte

Podría ser un proceso suyo hablando con el servicio por HTTP. No lo es, y por una razón
concreta: dos procesos serían **dos registros de runs**, y un run empezado desde Telegram
no existiría para ChatyGPT ni al revés. `identity.py` existe justamente para que una
persona en ChatyGPT y en Telegram sea la misma persona; darle dos runtimes separados
desmentiría eso desde el primer minuto.

Así que Telegram es una boca más del mismo servicio: mismo `RunRegistry`, mismo bus, mismos
runs. Lo que se ve en un sitio se puede cancelar desde el otro.

## Qué hace falta para arrancarlo

    ATHENA_TELEGRAM_TOKEN         el token del bot (o ATHENA_TELEGRAM_TOKEN_FILE)
    ATHENA_TELEGRAM_ALLOWED_IDS   quién puede hablarle, por id numérico de Telegram
    ATHENA_TELEGRAM_WORKSPACES    id:ruta;id:ruta — sobre qué carpeta trabaja cada uno

Las tres son obligatorias y ninguna tiene valor por defecto. Un bot sin lista de permitidos
sería un agente con shell atendiendo a cualquiera que le escriba, y un valor por defecto
razonable para eso no existe.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path

from athena.adapters.channel_gateway import (
    ChannelAccessPolicy,
    ChannelGrant,
    serve_channel,
)
from athena.adapters.service.launch import ServiceEndpoint, service_ready_line
from athena.adapters.service.runs import CapabilityMode, RunOptions
from athena_service import ServiceSettings, build_service
from athena_telegram.adapter import TelegramAdapter
from athena_telegram.api import TelegramApi
from athena_telegram.config import CHANNEL, TelegramConfigError, TelegramSecurity, resolve_token

WORKSPACES_VARIABLE = "ATHENA_TELEGRAM_WORKSPACES"

#: Lo que puede hacer un run pedido desde un chat, mientras no se diga otra cosa.
#:
#: Escribe, no ejecuta. La asimetría es deliberada: una escritura queda en el workspace y
#: se puede leer, revertir y discutir; un comando puede hacer cualquier cosa que pueda
#: hacer la máquina. Y nadie en un chat puede contestar una petición de permiso —ADR-009
#: quiere una persona decidiendo sobre una acción concreta, no un «sí» por mensajería—, así
#: que lo que quede en ASK se rechaza. Dárselo todo por comodidad sería conceder la
#: capacidad más peligrosa por el canal con la identidad más débil.
DEFAULT_GRANT_OPTIONS = RunOptions(
    writes=CapabilityMode.ALLOW,
    execution=CapabilityMode.OFF,
)


def parse_workspaces(raw: str | None) -> dict[str, ChannelGrant]:
    """`id:ruta;id:ruta` → a qué carpeta puede llegar cada persona.

    Por identidad y no una carpeta global: dos personas con acceso al bot no tienen por qué
    tener acceso al mismo código, y una sola raíz compartida convertiría el permiso de
    hablar con el bot en permiso sobre todo lo que el bot alcanza.
    """
    if not raw or not raw.strip():
        raise TelegramConfigError(
            f"{WORKSPACES_VARIABLE} hace falta: sin él nadie tiene carpeta sobre la que trabajar"
        )
    grants: dict[str, ChannelGrant] = {}
    for entrada in raw.split(";"):
        if not entrada.strip():
            continue
        identificador, separador, ruta = entrada.partition(":")
        # `rpartition` no vale: una ruta de Windows lleva dos puntos («D:/repo»), así que se
        # parte por el primero y el resto es la ruta entera.
        if not separador or not identificador.strip() or not ruta.strip():
            raise TelegramConfigError(f"Entrada mal formada en {WORKSPACES_VARIABLE}: {entrada!r}")
        clave = f"{CHANNEL}:{identificador.strip()}"
        grants[clave] = ChannelGrant(
            workspace_root=Path(ruta.strip()), options=DEFAULT_GRANT_OPTIONS
        )
    if not grants:
        raise TelegramConfigError(f"{WORKSPACES_VARIABLE} no contiene ninguna entrada")
    return grants


async def serve(settings: ServiceSettings, *, stop: asyncio.Event | None = None) -> None:
    """El servicio HTTP y el canal de Telegram, en el mismo proceso y sobre los mismos runs."""
    security = TelegramSecurity.from_environment()
    if not security.allowed_user_ids:
        raise TelegramConfigError(
            "ATHENA_TELEGRAM_ALLOWED_IDS hace falta: un bot sin lista de permitidos es un "
            "agente atendiendo a cualquiera que le escriba"
        )
    grants = parse_workspaces(os.environ.get(WORKSPACES_VARIABLE))
    sin_permiso = sorted(
        clave for clave in grants if _identificador(clave) not in security.allowed_user_ids
    )
    if sin_permiso:
        # Las dos listas existen para fallar por separado, pero una carpeta concedida a
        # quien el transporte no escucha es un descuido, no una defensa en profundidad:
        # quien la escribio creia estar dando acceso y no lo dio.
        raise TelegramConfigError(
            "Estas identidades tienen carpeta y no estan en la lista de permitidos: "
            + ", ".join(sin_permiso)
        )

    adapter = TelegramAdapter(TelegramApi(resolve_token()), security)
    service = build_service(settings)
    host, port = await service.start()
    endpoint = ServiceEndpoint(f"http://{host}:{port}", settings.service_token)
    print(service_ready_line(endpoint), flush=True)
    print(
        f'ATHENA_TELEGRAM_READY {{"identities":{len(grants)}}}',
        flush=True,
    )
    canal = asyncio.create_task(
        serve_channel(
            adapter,
            service.registry,
            ChannelAccessPolicy(grants),
            service.registry.event_bus,
            # Texto suelto sí arranca un run: aquí la lista de permitidos ya dice quién
            # habla y sobre qué carpeta, que es la ambigüedad que este interruptor cuida.
            bare_text_starts_run=True,
        )
    )
    parada = stop or asyncio.Event()
    try:
        await asyncio.wait(
            [canal, asyncio.create_task(parada.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        canal.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await canal
        await service.stop()


def _identificador(clave: str) -> int | None:
    """El id numerico de una clave `telegram:12345`, o `None` si no lo es.

    `None` y no una excepcion: una clave rara no esta en la lista de permitidos, que es
    exactamente lo que hay que concluir de ella.
    """
    _, _, crudo = clave.partition(":")
    try:
        return int(crudo)
    except ValueError:
        return None


def main() -> int:
    try:
        settings = ServiceSettings.from_environment()
        asyncio.run(serve(settings))
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as exc:
        print(f"No se pudo iniciar Telegram para Athena: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
