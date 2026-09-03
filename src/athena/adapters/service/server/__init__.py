"""Servicio HTTP/SSE local de Athena.

Partido en tres:

- `http`        — peticion, respuesta, limites y configuracion.
- `auxiliares`  — consulta, reanudacion de stream y emparejado de rutas.
- `servicio`    — el ciclo de conexion y los endpoints.
"""

from __future__ import annotations

from athena.adapters.service.server.http import Request, Response, ServiceConfig
from athena.adapters.service.server.servicio import AthenaService

__all__ = ["AthenaService", "Request", "Response", "ServiceConfig"]
