# Backbone - Carregamento dos certificados Apple
# Producao: variaveis de ambiente em base64. Local: arquivos em server/certs/

import os
import base64
import tempfile

CERTS_DIR = os.path.join(os.path.dirname(__file__), "certs")
_CACHE = {}


def _materializar(env_var, nome_arquivo):
    """Devolve o CAMINHO de um arquivo PEM em disco."""
    if nome_arquivo in _CACHE:
        return _CACHE[nome_arquivo]

    valor = os.environ.get(env_var)
    if valor:
        dados = base64.b64decode(valor)
        caminho = os.path.join(tempfile.gettempdir(), nome_arquivo)
        with open(caminho, "wb") as f:
            f.write(dados)
        _CACHE[nome_arquivo] = caminho
        return caminho

    caminho = os.path.join(CERTS_DIR, nome_arquivo)
    if os.path.exists(caminho):
        _CACHE[nome_arquivo] = caminho
        return caminho

    raise RuntimeError(
        f"Certificado ausente: defina {env_var} (base64) ou coloque {nome_arquivo} em server/certs/"
    )


def caminho_cert():
    return _materializar("APPLE_CERT_B64", "backbone_cert.pem")


def caminho_key():
    return _materializar("APPLE_KEY_B64", "backbone_key.pem")


def caminho_wwdr():
    return _materializar("APPLE_WWDR_B64", "wwdr.pem")


def ler_cert():
    with open(caminho_cert(), "rb") as f:
        return f.read()


def ler_key():
    with open(caminho_key(), "rb") as f:
        return f.read()


def ler_wwdr():
    with open(caminho_wwdr(), "rb") as f:
        return f.read()
