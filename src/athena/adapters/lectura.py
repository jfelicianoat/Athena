"""Leer la respuesta de un proveedor sin dejar que decida cuanta memoria gasta Athena.

`response.read()` lee lo que el servidor quiera mandar. Recortar despues protege el
contexto del modelo, pero no el pico de memoria al recibirlo (auditoria A21). Aqui se lee
como mucho un techo y, si hay mas, se rechaza con un error que lo dice.
"""

from __future__ import annotations

import http.client

from athena.errors import ModelPermanentError

#: Ninguna respuesta de un modelo o del broker necesita mas. Una que lo supere es un
#: servidor que se ha equivocado de sitio, no una respuesta larga.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


def read_bounded(response: http.client.HTTPResponse, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ModelPermanentError(
            f"The provider response exceeds {limit // (1024 * 1024)} MiB and was not read",
            details={"limit_bytes": limit},
        )
    return data


__all__ = ["MAX_RESPONSE_BYTES", "read_bounded"]
