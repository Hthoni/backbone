# Backbone Beer — Armazenamento de dados no Google Cloud Storage.
# v3: registros de dispositivo (push), config de meta e temporada.

import json
import os
import uuid
import concurrent.futures
from google.cloud import storage

BUCKET_NAME = "backbone-consumidor"


def _bucket():
    client = storage.Client()
    return client.bucket(BUCKET_NAME)


def _listar_prefixo(prefixo):
    """
    Lista e baixa em PARALELO todos os .json sob um prefixo do bucket.

    Ate aqui, cada listar_X() baixava um arquivo de cada vez, em fila —
    com centenas de sócios (o caso mais comum), isso deixava qualquer
    tela do gestor/admin visivelmente lenta, porque cada consulta
    esperava dezenas/centenas de idas e vindas sequenciais ao bucket.
    E o MESMO problema que ja resolvemos nos eventos (log.jsonl) —
    so que aqui nao da pra juntar tudo num arquivo so, porque cada
    consumidor/garcom/etc precisa continuar sendo lido e escrito
    individualmente o tempo todo (carregar_consumidor, scan, etc.).
    A correcao aqui e diferente: baixar todos ao mesmo tempo (varias
    conexoes simultaneas) em vez de um de cada vez — como e tudo
    espera de rede e nao processamento, isso da um ganho grande sem
    mudar onde nem como os dados sao guardados.
    """
    client = storage.Client()
    blobs = [b for b in client.list_blobs(BUCKET_NAME, prefix=prefixo) if b.name.endswith(".json")]

    def _baixar(blob):
        try:
            return json.loads(blob.download_as_text())
        except Exception:
            return None

    resultados = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        for r in executor.map(_baixar, blobs):
            if r is not None:
                resultados.append(r)
    return resultados


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
    return _listar_prefixo("consumidores/")


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
#
# ATE v3: cada evento era 1 arquivo (eventos/{uuid}.json). Funcionava
# bem com poucos eventos, mas toda consulta (resumo do bar, historico,
# feed de atividade) precisava listar E BAIXAR cada arquivo, um por
# um — com centenas de eventos, cada tela do painel do gestor levava
# dezenas de requisicoes sequenciais ao bucket. Era a lentidao que o
# Henrique sentiu ao navegar.
#
# NOVO (v4): um unico arquivo-log (eventos/log.jsonl, formato NDJSON —
# uma linha = um evento em JSON). salvar_evento baixa o log inteiro,
# acrescenta a linha nova, e regrava. listar_eventos vira 1 download
# so, nao importa quantos eventos existam. Troca N requisicoes por 1.
#
# Contrapartida honesta: como salvar_evento faz download+upload do
# arquivo inteiro, duas gravacoes SIMULTANEAS podem se atropelar (a
# segunda sobrescreve sem ver a primeira). Com o volume atual (scans
# de garcom, um de cada vez) o risco e baixo. Se o clube crescer muito
# e isso virar problema de verdade, o proximo passo e Firestore em vez
# de arquivo — anotado aqui para quando chegar a hora, igual as outras
# notas de "solucao simples tem prazo de validade" do projeto.

_LOG_EVENTOS = "eventos/log.jsonl"


def salvar_evento(evento: dict):
    evento.setdefault("id", str(uuid.uuid4()))
    linha = json.dumps(evento, ensure_ascii=False)

    blob = _bucket().blob(_LOG_EVENTOS)
    atual = blob.download_as_text() if blob.exists() else ""
    if atual and not atual.endswith("\n"):
        atual += "\n"
    blob.upload_from_string(atual + linha + "\n", content_type="application/x-ndjson")


def listar_eventos():
    """Lista todos os eventos gravados (para estatisticas). 1 download so."""
    blob = _bucket().blob(_LOG_EVENTOS)
    if not blob.exists():
        return []
    eventos = []
    for linha in blob.download_as_text().splitlines():
        linha = linha.strip()
        if not linha:
            continue
        try:
            eventos.append(json.loads(linha))
        except Exception:
            continue
    return eventos


def listar_eventos_consumidor(telefone: str):
    eventos = [e for e in listar_eventos() if e.get("telefone") == telefone]
    return sorted(eventos, key=lambda x: x.get("data", ""))


def migrar_eventos_para_log():
    """
    Migracao unica (rodar 1 vez): le todos os eventos antigos, gravados
    como eventos/{uuid}.json, e os junta no eventos/log.jsonl novo.
    NAO apaga os arquivos antigos — so deixa de le-los depois que o
    log novo existir. Seguro rodar mais de uma vez (ignora se o
    log ja tiver conteudo, para nao duplicar).
    """
    blob_log = _bucket().blob(_LOG_EVENTOS)
    if blob_log.exists() and blob_log.download_as_text().strip():
        return {"status": "ja_migrado", "motivo": "log.jsonl ja tem conteudo"}

    client = storage.Client()
    blobs = client.list_blobs(BUCKET_NAME, prefix="eventos/")
    eventos = []
    for b in blobs:
        if b.name == _LOG_EVENTOS or not b.name.endswith(".json"):
            continue
        try:
            eventos.append(json.loads(b.download_as_text()))
        except Exception:
            continue

    eventos.sort(key=lambda e: e.get("data", ""))
    linhas = "\n".join(json.dumps(e, ensure_ascii=False) for e in eventos)
    if linhas:
        linhas += "\n"
    blob_log.upload_from_string(linhas, content_type="application/x-ndjson")

    return {"status": "ok", "migrados": len(eventos)}


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
    return _listar_prefixo("garcons/")


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
    return _listar_prefixo("bares/")


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


# ── Gestores (dono/gerente do bar — painel Historico/Atual/Atividade/Garcons) ──

def salvar_gestor(dados: dict):
    blob = _bucket().blob(f"gestores/{dados['id']}.json")
    blob.upload_from_string(json.dumps(dados, ensure_ascii=False), content_type="application/json")


def carregar_gestor(gestor_id: str):
    blob = _bucket().blob(f"gestores/{gestor_id}.json")
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def listar_gestores():
    return _listar_prefixo("gestores/")


def apagar_gestor(gestor_id: str):
    blob = _bucket().blob(f"gestores/{gestor_id}.json")
    if blob.exists():
        blob.delete()


# ── Admins (acesso total — admin.html + gestor.html como master) ──

def salvar_admin(dados: dict):
    blob = _bucket().blob(f"admins/{dados['id']}.json")
    blob.upload_from_string(json.dumps(dados, ensure_ascii=False), content_type="application/json")


def carregar_admin(admin_id: str):
    blob = _bucket().blob(f"admins/{admin_id}.json")
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def listar_admins():
    return _listar_prefixo("admins/")


def apagar_admin(admin_id: str):
    blob = _bucket().blob(f"admins/{admin_id}.json")
    if blob.exists():
        blob.delete()


# ── Colabs (parceiros externos que cadastram clientes por QR) ──

def salvar_colab(dados: dict):
    blob = _bucket().blob(f"colabs/{dados['id']}.json")
    blob.upload_from_string(json.dumps(dados, ensure_ascii=False), content_type="application/json")


def carregar_colab(colab_id: str):
    blob = _bucket().blob(f"colabs/{colab_id}.json")
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def listar_colabs():
    return _listar_prefixo("colabs/")


def apagar_colab(colab_id: str):
    blob = _bucket().blob(f"colabs/{colab_id}.json")
    if blob.exists():
        blob.delete()


def carregar_bloqueados():
    """
    Lista de bloqueios de associacao (antifraude), POR BAR.
    Cada item: {"telefone": "5521...", "bar": "<bar_id>" ou "*" (todos)}.
    Entradas antigas em string sao tratadas como bar "*".
    """
    blob = _bucket().blob("bloqueados.json")
    if not blob.exists():
        return []
    try:
        bruto = json.loads(blob.download_as_text())
    except Exception:
        return []
    lista = []
    for item in bruto:
        if isinstance(item, str):
            lista.append({"telefone": item, "bar": "*"})
        elif isinstance(item, dict) and item.get("telefone"):
            lista.append({"telefone": item["telefone"], "bar": item.get("bar") or "*"})
    return lista


def salvar_bloqueados(lista):
    vistos, limpa = set(), []
    for item in lista:
        chave = (item["telefone"], item.get("bar") or "*")
        if chave in vistos:
            continue
        vistos.add(chave)
        limpa.append({"telefone": chave[0], "bar": chave[1]})
    limpa.sort(key=lambda x: (x["bar"], x["telefone"]))
    _bucket().blob("bloqueados.json").upload_from_string(
        json.dumps(limpa), content_type="application/json")


# ── Mensagens (canal livre do socio — pedido de resgate, duvida, etc.) ──
#
# Mesmo padrao NDJSON dos eventos: 1 arquivo-log, 1 download pra listar.
# Diferente dos eventos, aqui PRECISA editar uma linha especifica depois
# (marcar como lida), entao marcar_mensagem_lida faz read-modify-write
# do arquivo inteiro — aceitavel pelo volume baixo esperado (mensagens
# avulsas de socio, nao um evento por scan).

_LOG_MENSAGENS = "mensagens/log.jsonl"


def salvar_mensagem(mensagem: dict):
    mensagem.setdefault("id", str(uuid.uuid4()))
    mensagem.setdefault("lida", False)
    linha = json.dumps(mensagem, ensure_ascii=False)

    blob = _bucket().blob(_LOG_MENSAGENS)
    atual = blob.download_as_text() if blob.exists() else ""
    if atual and not atual.endswith("\n"):
        atual += "\n"
    blob.upload_from_string(atual + linha + "\n", content_type="application/x-ndjson")
    return mensagem


def listar_mensagens():
    blob = _bucket().blob(_LOG_MENSAGENS)
    if not blob.exists():
        return []
    mensagens = []
    for linha in blob.download_as_text().splitlines():
        linha = linha.strip()
        if not linha:
            continue
        try:
            mensagens.append(json.loads(linha))
        except Exception:
            continue
    return mensagens


def marcar_mensagem_lida(mensagem_id: str):
    """Le o log inteiro, marca a mensagem certa como lida, e regrava tudo."""
    mensagens = listar_mensagens()
    achou = False
    for m in mensagens:
        if m.get("id") == mensagem_id:
            m["lida"] = True
            achou = True
            break
    if not achou:
        return False

    linhas = "\n".join(json.dumps(m, ensure_ascii=False) for m in mensagens)
    if linhas:
        linhas += "\n"
    _bucket().blob(_LOG_MENSAGENS).upload_from_string(linhas, content_type="application/x-ndjson")
    return True
