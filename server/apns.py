# Backbone - Push para o Apple Wallet via APNs
#
# O push do Wallet e SILENCIOSO: payload vazio {}.
# Ele apenas avisa o iPhone "tem coisa nova" - o iPhone entao chama
# GET /v1/passes/... e baixa o .pkpass atualizado. O texto que o usuario
# ve na tela vem do changeMessage dentro do pass.json, nao daqui.
#
# Autenticacao: o MESMO certificado do Pass Type ID (TLS client cert).
# Nao e preciso criar nenhuma chave nova na Apple.

import os
import json
import httpx
import certs
import storage

APNS_HOST = os.environ.get("APNS_HOST", "https://api.push.apple.com")
TOPIC = os.environ.get("PASS_TYPE_ID", "pass.com.backbonecervejaria.clube")

_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.Client(
            http2=True,
            cert=(certs.caminho_cert(), certs.caminho_key()),
            timeout=10.0,
        )
    return _CLIENT


def enviar_push(serial: str):
    """
    Avisa todos os dispositivos registrados para este serial (telefone).
    Retorna um resumo do que aconteceu.
    """
    registro = storage.carregar_registro(serial)
    if not registro:
        return {"enviados": 0, "motivo": "nenhum_dispositivo"}

    dispositivos = registro.get("dispositivos", {})
    if not dispositivos:
        return {"enviados": 0, "motivo": "nenhum_dispositivo"}

    enviados, falhas, removidos = 0, [], []

    for device_id, push_token in list(dispositivos.items()):
        try:
            r = _client().post(
                f"{APNS_HOST}/3/device/{push_token}",
                headers={
                    "apns-topic": TOPIC,
                    "apns-push-type": "background",
                    "apns-priority": "5",
                },
                content=json.dumps({}),
            )
            if r.status_code == 200:
                enviados += 1
            elif r.status_code == 410:
                # dispositivo nao existe mais - limpa
                storage.remover_registro(device_id, serial)
                removidos.append(device_id)
            else:
                falhas.append({"device": device_id, "status": r.status_code, "corpo": r.text})
        except Exception as e:
            falhas.append({"device": device_id, "erro": str(e)})

    return {"enviados": enviados, "falhas": falhas, "removidos": removidos}
