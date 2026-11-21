"""Utilidades para probar contra el AI_Broker REAL (auditoría 19-nov-2026).

El token del Broker rota en cada arranque; se pide a AI OS Core con una lectura
(`GET /api/v1/credentials/broker`) usando la conexión que Core deja para Athena. No se
escribe nada en Core y el token nunca va por línea de órdenes ni a disco.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from athena.adapters.ai_broker import AiBrokerModelProvider
from athena.adapters.ai_os_core import CoreBrokerToken, core_connection
from athena.cancellation import CancellationSource

BROKER_URL = os.environ.get("AUDIT_BROKER_URL", "https://192.168.1.52:8765")


def broker_token() -> str:
    found = core_connection()
    if found is None:
        raise SystemExit("No hay conexión de Core para athena; no se puede obtener el token")
    token = CoreBrokerToken(found[0], found[1])()
    if not token:
        raise SystemExit("Core no ha dado el token del Broker")
    return token


async def probe(token: str) -> None:
    provider = AiBrokerModelProvider(BROKER_URL, token)
    cancel = CancellationSource().token
    health = await provider.health(cancel)
    print("health:", health.status.value, health.detail)
    print("credencial:", await provider.verify_credentials(cancel))
    catalogue = await provider.model_capabilities(cancel)
    usable = sorted(name for name, caps in catalogue.items() if "completion" in caps)
    print(f"modelos: {len(catalogue)}; con completion: {len(usable)}")
    for name in usable:
        print("  ", name)
    print("cola:", await provider.queue_load(cancel))


if __name__ == "__main__":
    asyncio.run(probe(broker_token()))
