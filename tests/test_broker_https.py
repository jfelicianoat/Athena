"""El Broker en HTTPS: su certificado lo firma la CA de Agora, que se confía solo para él."""
from __future__ import annotations

import ssl

import pytest

from athena.adapters.ai_broker import broker_ssl_context

# Certificado público (no la clave) de la CA de Agora del PC principal.
AGORA_CA = """-----BEGIN CERTIFICATE-----
MIIBzjCCAVSgAwIBAgIUCWVS5X4G5SAA06CvKLJbtzJ/pfowCgYIKoZIzj0EAwMw
JTEjMCEGA1UEAwwaQWdvcmEgQ0EgKERFU0tUT1AtQ0FRNVNMNSkwHhcNMjYwOTA1
MTg1NzAxWhcNMzYwOTAyMTkwMjAxWjAlMSMwIQYDVQQDDBpBZ29yYSBDQSAoREVT
S1RPUC1DQVE1U0w1KTB2MBAGByqGSM49AgEGBSuBBAAiA2IABGjSG+mHRrtAnKXf
z2eoSodinnooPMhbzE408k2LhImZRKiAXA6/dvkzScEWv1/THjsFQzvd2wPhTQ4X
GKhsZDfY3I5qsx49L+k99o2TvON46JU/Ow80HlA04ddbFdcAjaNFMEMwEgYDVR0T
AQH/BAgwBgEB/wIBADAOBgNVHQ8BAf8EBAMCAYYwHQYDVR0OBBYEFAuD8n+MC/oF
603eCbrPhgcfBFXiMAoGCCqGSM49BAMDA2gAMGUCMQD/88o808uBTfWc+oQv4Lm+
MEMhAQrGg5fgyp6JtN7dqmTNgWMmYEpHD05AhR7gMC0CMHklfjnJ9GJhdq/iXhtK
+QA8Tj9xKhFCs9X8zXUOuuuZSqs3SEYYrT+fXL3pVxcLPw==
-----END CERTIFICATE-----
"""


def test_confia_en_la_ca_indicada_sin_dejar_de_verificar(tmp_path, monkeypatch) -> None:
    ca = tmp_path / "ca.crt"
    ca.write_text(AGORA_CA, encoding="ascii")
    monkeypatch.setenv("AI_BROKER_CA_CERT", str(ca))
    context = broker_ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    nombres = [dict(par[0] for par in c["subject"]).get("commonName", "") for c in context.get_ca_certs()]
    assert any(n.startswith("Agora CA") for n in nombres)


def test_una_ca_indicada_que_no_existe_es_un_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AI_BROKER_CA_CERT", str(tmp_path / "no-existe.crt"))
    with pytest.raises(FileNotFoundError):
        broker_ssl_context()
