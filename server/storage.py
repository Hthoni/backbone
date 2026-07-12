# Backbone Beer — Armazenamento de dados no Google Cloud Storage.

import json
import os
from google.cloud import storage

BUCKET_NAME = "backbone-consumidor"

def _bucket():
    client = storage.Client()
    return client.bucket(BUCKET_NAME)

# ── Consumidores ──────────────────────────────────────────────

def salvar_consumidor(dados: dict):
    """Salva ou atualiza o perfil de um consumidor pelo telefone."""
    tel = dados["telefone"]
    blob = _bucket().blob(f"consumidores/{tel}.json")
    blob.upload_from_string(json.dumps(dados, ensure_ascii=False), content_type="application/json")

def carregar_consumidor(telefone: str):
    """Carrega o perfil de um consumidor. Retorna None se não existir."""
    blob = _bucket().blob(f"consumidores/{telefone}.json")
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())

def listar_consumidores():
    """Lista todos os consumidores cadastrados."""
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

# ── Logs de eventos ───────────────────────────────────────────

def salvar_evento(evento: dict):
    """Salva um evento de punch ou resgate no log."""
    import uuid
    evento_id = str(uuid.uuid4())
    blob = _bucket().blob(f"eventos/{evento_id}.json")
    blob.upload_from_string(json.dumps(evento, ensure_ascii=False), content_type="application/json")

def listar_eventos_consumidor(telefone: str):
    """Lista todos os eventos de um consumidor específico."""
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
    """Salva ou atualiza o perfil de um garçom."""
    gid = dados["id"]
    blob = _bucket().blob(f"garcons/{gid}.json")
    blob.upload_from_string(json.dumps(dados, ensure_ascii=False), content_type="application/json")

def carregar_garcom(garcom_id: str):
    """Carrega um garçom pelo ID. Retorna None se não existir."""
    blob = _bucket().blob(f"garcons/{garcom_id}.json")
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())

def listar_garcons():
    """Lista todos os garçons cadastrados."""
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
    """Salva ou atualiza um bar parceiro."""
    bid = dados["id"]
    blob = _bucket().blob(f"bares/{bid}.json")
    blob.upload_from_string(json.dumps(dados, ensure_ascii=False), content_type="application/json")

def carregar_bar(bar_id: str):
    """Carrega um bar pelo ID. Retorna None se não existir."""
    blob = _bucket().blob(f"bares/{bar_id}.json")
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())

def listar_bares():
    """Lista todos os bares cadastrados."""
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
    """Carrega as configurações gerais do programa."""
    blob = _bucket().blob("config/config.json")
    if not blob.exists():
        return {
            "punches_para_recompensa": 10,
            "delay_resgate_horas": 24
        }
    return json.loads(blob.download_as_text())

def salvar_config(config: dict):
    """Salva as configurações gerais do programa."""
    blob = _bucket().blob("config/config.json")
    blob.upload_from_string(json.dumps(config, ensure_ascii=False), content_type="application/json")
