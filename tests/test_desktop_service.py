from __future__ import annotations

import http.client
from pathlib import Path

import pytest

from athena_desktop.service import (
    ManagedServiceRequest,
    ServiceAlreadyRunning,
    start_managed_service,
)


def test_desktop_starts_athena_and_captures_its_generated_token(tmp_path: Path) -> None:
    managed = start_managed_service(
        ManagedServiceRequest(
            broker_base_url="http://127.0.0.1:9",
            broker_token="broker-test-only",
            state_dir=tmp_path,
            port=0,
        ),
        timeout_seconds=10,
    )
    try:
        assert managed.endpoint.token
        connection = http.client.HTTPConnection(
            managed.endpoint.host,
            managed.endpoint.port,
            timeout=2,
        )
        connection.request("GET", "/v1/health")
        response = connection.getresponse()
        response.read()
        connection.close()
        assert response.status == 200
    finally:
        managed.stop()


def test_desktop_attaches_first_and_does_not_start_a_second_athena(tmp_path: Path) -> None:
    first = start_managed_service(
        ManagedServiceRequest(
            broker_base_url="http://127.0.0.1:9",
            broker_token="broker-test-only",
            state_dir=tmp_path / "first",
            port=0,
        ),
        timeout_seconds=10,
    )
    try:
        with pytest.raises(ServiceAlreadyRunning) as caught:
            start_managed_service(
                ManagedServiceRequest(
                    broker_base_url="http://127.0.0.1:9",
                    broker_token="broker-test-only",
                    state_dir=tmp_path / "second",
                    port=first.endpoint.port,
                ),
                timeout_seconds=10,
            )

        assert caught.value.base_url == first.endpoint.base_url
        assert first.process.poll() is None
    finally:
        first.stop()
