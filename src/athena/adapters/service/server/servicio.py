"""El servicio: compone transporte y endpoints en una sola clase."""

from __future__ import annotations

from athena.adapters.service.server.endpoints_varios import EndpointsVariosMixin


class AthenaService(EndpointsVariosMixin):
    """Servicio HTTP/SSE local de Athena."""
