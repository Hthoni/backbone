# Backbone Beer — Armazenamento de dados no Google Cloud Storage.
# v3: registros de dispositivo (push), config de meta e temporada.

import json
import os
import uuid
from google.cloud import storage

BUCKET_NAME = "backbone-consumidor"


def _bucket():
    client = storage.Client()
    return client.bucket(BUCKET_NAME)


# ── Consumidores ──────────────────────────────────────────────

def salvar_consumidor(dados: dict):
    tel = dados["telefone"]
    blob = _bucket().blob(f"consumidores/{tel}.json")
    blob.upload_from_string(json.dumps(dados, ensure_ascii=False), content_type="application/json")


def carregar_consumidor(telefone: str):
    blob = _bucket().blob(f"consumidores/{telefone}.json")
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def listar_consumidores():
    client = storage.Client()
    blobs = client.list_blobs(BUCKET_NAME, prefix="consumidores/")
    consumidores = []
    for blob in blobs:
        if blob.name.endswith(".json"):
            try:
                consumidores.append(json.loads(blob.download_as_text()))
            except Exception:
                pass
    return consumidores


# ── Registros de dispositivo (Apple Wallet push) ──────────────
#
# Dois indices, porque a Apple consulta pelos dois lados:
#   registros/{serial}.json      -> {"dispositivos": {deviceId: pushToken}, "atualizado_em": ...}
#   dispositivos/{deviceId}.json -> {"seriais": ["5521...", ...]}

def carregar_registro(serial: str):
    blob = _bucket().blob(f"registros/{serial}.json")
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def salvar_registro(serial: str, device_id: str, push_token: str):
    reg = carregar_registro(serial) or {"serial": serial, "dispositivos": {}}
    reg["dispositivos"][device_id] = push_token
    _bucket().blob(f"registros/{serial}.json").upload_from_string(
        json.dumps(reg, ensure_ascii=False), content_type="application/json")

    blob = _bucket().blob(f"dispositivos/{device_id}.json")
    disp = json.loads(blob.download_as_text()) if blob.exists() else {"device_id": device_id, "seriais": []}
    if serial not in disp["seriais"]:
        disp["seriais"].append(serial)
    blob.upload_from_string(json.dumps(disp, ensure_ascii=False), content_type="application/json")


def remover_registro(device_id: str, serial: str):
    reg = carregar_registro(serial)
    if reg and device_id in reg.get("dispositivos", {}):
        del reg["dispositivos"][device_id]
        _bucket().blob(f"registros/{serial}.json").upload_from_string(
            json.dumps(reg, ensure_ascii=False), content_type="application/json")

    blob = _bucket().blob(f"dispositivos/{device_id}.json")
    if blob.exists():
        disp = json.loads(blob.download_as_text())
        disp["seriais"] = [s for s in disp.get("seriais", []) if s != serial]
        blob.upload_from_string(json.dumps(disp, ensure_ascii=False), content_type="application/json")


def seriais_do_dispositivo(device_id: str):
    blob = _bucket().blob(f"dispositivos/{device_id}.json")
    if not blob.exists():
        return []
    return json.loads(blob.download_as_text()).get("seriais", [])


# ── Logs de eventos ───────────────────────────────────────────

def salvar_evento(evento: dict):
    evento_id = str(uuid.uuid4())
    blob = _bucket().blob(f"eventos/{evento_id}.json")
    blob.upload_from_string(json.dumps(evento, ensure_ascii=False), content_type="application/json")


def listar_eventos_consumidor(telefone: str):
    client = storage.Client()
    blobs = client.list_blobs(BUCKET_NAME, prefix="eventos/")
    eventos = []
    for blob in blobs:
        if blob.name.endswith(".json"):
            try:
                ev = json.loads(blob.download_as_text())
                if ev.get("telefone") == telefone:
                    eventos.append(ev)
            except Exception:
                pass
    return sorted(eventos, key=lambda x: x.get("data", ""))


# ── Garçons ───────────────────────────────────────────────────

def salvar_garcom(dados: dict):
    blob = _bucket().blob(f"garcons/{dados['id']}.json")
    blob.upload_from_string(json.dumps(dados, ensure_ascii=False), content_type="application/json")


def carregar_garcom(garcom_id: str):
    blob = _bucket().blob(f"garcons/{garcom_id}.json")
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def listar_garcons():
    client = storage.Client()
    blobs = client.list_blobs(BUCKET_NAME, prefix="garcons/")
    garcons = []
    for blob in blobs:
        if blob.name.endswith(".json"):
            try:
                garcons.append(json.loads(blob.download_as_text()))
            except Exception:
                pass
    return garcons


# ── Bares ─────────────────────────────────────────────────────

def salvar_bar(dados: dict):
    blob = _bucket().blob(f"bares/{dados['id']}.json")
    blob.upload_from_string(json.dumps(dados, ensure_ascii=False), content_type="application/json")


def carregar_bar(bar_id: str):
    blob = _bucket().blob(f"bares/{bar_id}.json")
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def listar_bares():
    client = storage.Client()
    blobs = client.list_blobs(BUCKET_NAME, prefix="bares/")
    bares = []
    for blob in blobs:
        if blob.name.endswith(".json"):
            try:
                bares.append(json.loads(blob.download_as_text()))
            except Exception:
                pass
    return bares


# ── Config geral ──────────────────────────────────────────────

def carregar_config():
    blob = _bucket().blob("config/config.json")
    if not blob.exists():
        return {
            "punches_para_recompensa": 10,
            "temporada_vigente": "padrao",
        }
    cfg = json.loads(blob.download_as_text())
    cfg.setdefault("punches_para_recompensa", 10)
    cfg.setdefault("temporada_vigente", "padrao")
    return cfg


def salvar_config(config: dict):
    blob = _bucket().blob("config/config.json")
    blob.upload_from_string(json.dumps(config, ensure_ascii=False), content_type="application/json")

def apagar_consumidor(telefone: str):
    """Apaga o consumidor e o registro de push dele."""
    b = _bucket()
    for caminho in (f"consumidores/{telefone}.json", f"registros/{telefone}.json"):
        blob = b.blob(caminho)
        if blob.exists():
            blob.delete()


def apagar_bar(bar_id: str):
    blob = _bucket().blob(f"bares/{bar_id}.json")
    if blob.exists():
        blob.delete()


def apagar_garcom(garcom_id: str):
    blob = _bucket().blob(f"garcons/{garcom_id}.json")
    if blob.exists():
        blob.delete()
