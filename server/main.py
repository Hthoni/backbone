# Backbone Beer - Clube Backbone - v3
#
# Mudancas em relacao a v2:
#   - /scan unificado: o garcom faz UM gesto. Se ha recompensa, resgata.
#     Se nao ha, pontua. A cartela SO zera no resgate.
#   - meta e temporada congeladas por ciclo
#   - member gets member: indicado_por / indicados
#   - web service da Apple (4 endpoints) + push via APNs
#   - /cartao/<telefone>: entrega o .pkpass

import os
import json
import uuid
import secrets
import re
from datetime import datetime, timezone
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

import storage
import pkpass
import apns

app = Flask(__name__)
CORS(app)

PASS_TYPE_ID = os.environ.get("PASS_TYPE_ID", "pass.com.backbonecervejaria.clube")


def agora():
    return datetime.now(timezone.utc).isoformat()


def so_digitos(s):
    return "".join(c for c in (s or "") if c.isdigit())


def _disponiveis(c):
    return [r for r in c.get("recompensas", []) if not r.get("resgatada")]


def _garcom_pertence_bar(garcom, bar):
    return garcom.get("bar_id") == bar


def _notificar(consumidor, aviso):
    """Grava o aviso no consumidor e dispara o push."""
    consumidor["aviso"] = aviso
    consumidor["atualizado_em"] = agora()
    storage.salvar_consumidor(consumidor)
    try:
        return apns.enviar_push(consumidor["telefone"])
    except Exception as e:
        app.logger.warning("push falhou: %s", e)
        return {"enviados": 0, "erro": str(e)}


# ══════════════════════════════════════════════════════════════
#  CADASTRO
# ══════════════════════════════════════════════════════════════

@app.route("/cadastro/<bar>/<telefone>/<nome>/<dia>/<mes>/<time>")
def cadastro(bar, telefone, nome, dia, mes, time):
    from urllib.parse import unquote
    nome = unquote(nome)
    bar = unquote(bar)
    time = unquote(time)
    telefone = so_digitos(telefone)

    # indicador vem por query string: ?ind=5521999887766
    indicador_tel = so_digitos(request.args.get("ind", ""))
    if indicador_tel == telefone:
        indicador_tel = ""  # auto-indicacao bloqueada

    existente = storage.carregar_consumidor(telefone)
    if existente:
        return jsonify({"status": "ja_cadastrado", "consumidor": existente})

    config = storage.carregar_config()

    indicador = storage.carregar_consumidor(indicador_tel) if indicador_tel else None

    consumidor = {
        "telefone": telefone,
        "nome": nome,
        "bar_origem": bar,
        "nascimento_dia": dia,
        "nascimento_mes": mes,
        "time": time,

        "punches": 0,
        "meta": int(config["punches_para_recompensa"]),        # CONGELADA
        "temporada": config["temporada_vigente"],              # CONGELADA

        "indicado_por": indicador["telefone"] if indicador else None,
        "indicados": [],

        "recompensas": [
            {"tipo": "boas_vindas", "gerada_em": agora(), "resgatada": False}
        ],
        "recompensas_total": 0,
        "ciclos_completos": 0,
        "temporadas_completas": [],

        "auth_token": secrets.token_hex(20),   # unico por consumidor
        "aviso": "Bem-vindo ao Clube Backbone! Seu primeiro chopp é por nossa conta.",
        "ultimo_punch": None,
        "cadastro_em": agora(),
        "atualizado_em": agora(),
        "ativo": True,
    }
    storage.salvar_consumidor(consumidor)

    if indicador:
        indicador.setdefault("indicados", [])
        if telefone not in indicador["indicados"]:
            indicador["indicados"].append(telefone)
        _notificar(indicador, f"{nome} entrou no Clube pelo seu convite. Quando fechar a cartela, você ganha um ponto.")

    storage.salvar_evento({
        "id": str(uuid.uuid4()), "tipo": "cadastro", "telefone": telefone,
        "bar": bar, "garcom_id": None, "indicado_por": consumidor["indicado_por"],
        "data": agora(),
    })

    return jsonify({"status": "cadastrado", "consumidor": consumidor})


@app.route("/consumidor/<telefone>")
def consultar_consumidor(telefone):
    c = storage.carregar_consumidor(so_digitos(telefone))
    if not c:
        return jsonify({"erro": "nao_encontrado"}), 404
    c["punches_para_recompensa"] = c.get("meta", 10)
    c["recompensas_disponiveis"] = len(_disponiveis(c))
    c["total_indicados"] = len(c.get("indicados", []))
    return jsonify(c)


# ══════════════════════════════════════════════════════════════
#  SCAN - o gesto unico do garcom
# ══════════════════════════════════════════════════════════════

def _creditar_indicador(consumidor):
    """Chamado quando o consumidor FECHA a cartela. Credita 1 ponto ao indicador."""
    tel_ind = consumidor.get("indicado_por")
    if not tel_ind:
        return

    ind = storage.carregar_consumidor(tel_ind)
    if not ind or not ind.get("ativo", True):
        return

    # cartela cheia -> ponto DESCARTADO
    if _disponiveis(ind):
        _notificar(ind, "Um indicado seu fechou a meta, mas sua cartela está cheia. "
                        "Venha resgatar seu chopp para voltar a pontuar.")
        storage.salvar_evento({
            "id": str(uuid.uuid4()), "tipo": "ponto_indicacao_descartado",
            "telefone": tel_ind, "origem": consumidor["telefone"], "data": agora(),
        })
        return

    meta_ind = int(ind.get("meta", 10))
    ind["punches"] = int(ind.get("punches", 0)) + 1

    if ind["punches"] >= meta_ind:
        ind["recompensas"].append({"tipo": "punch", "gerada_em": agora(), "resgatada": False})
        ind["recompensas_total"] = ind.get("recompensas_total", 0) + 1
        aviso = ("Um indicado seu fechou a meta — e fechou a sua cartela! "
                 "Chame o atendente e resgate seu chopp.")
    else:
        faltam = meta_ind - ind["punches"]
        aviso = f"Parabéns! Seu indicado fechou a meta e você ganhou um ponto. Faltam {faltam} para o seu chopp."

    _notificar(ind, aviso)
    storage.salvar_evento({
        "id": str(uuid.uuid4()), "tipo": "ponto_indicacao",
        "telefone": tel_ind, "origem": consumidor["telefone"], "data": agora(),
    })


@app.route("/scan", methods=["POST"])
def scan():
    dados = request.get_json() or {}
    telefone = so_digitos(dados.get("telefone"))
    garcom_id = dados.get("garcom_id")
    bar = dados.get("bar")

    if not telefone or not garcom_id or not bar:
        return jsonify({"erro": "dados_incompletos"}), 400

    consumidor = storage.carregar_consumidor(telefone)
    if not consumidor:
        return jsonify({"erro": "consumidor_nao_encontrado"}), 404

    garcom = storage.carregar_garcom(garcom_id)
    if not garcom:
        return jsonify({"erro": "garcom_nao_encontrado"}), 404
    if not _garcom_pertence_bar(garcom, bar):
        return jsonify({"erro": "bar_diferente"}), 403

    config = storage.carregar_config()
    consumidor.setdefault("recompensas", [])
    consumidor.setdefault("meta", int(config["punches_para_recompensa"]))
    consumidor.setdefault("temporada", config["temporada_vigente"])

    pendentes = _disponiveis(consumidor)

    # ─────────── CAMINHO 1: tem recompensa -> RESGATA ───────────
    if pendentes:
        pendentes.sort(key=lambda r: 0 if r.get("tipo") == "boas_vindas" else 1)
        recompensa = pendentes[0]

        for r in consumidor["recompensas"]:
            if r is recompensa:
                r["resgatada"] = True
                r["resgatada_em"] = agora()
                r["resgatada_bar"] = bar
                r["resgatada_garcom"] = garcom_id
                break

        tipo = recompensa.get("tipo")

        if tipo == "punch":
            # fecha o ciclo: nova cartela, nova meta, nova temporada
            temporada_antiga = consumidor.get("temporada")
            consumidor["punches"] = 0
            consumidor["meta"] = int(config["punches_para_recompensa"])
            consumidor["temporada"] = config["temporada_vigente"]
            consumidor["ciclos_completos"] = consumidor.get("ciclos_completos", 0) + 1
            consumidor.setdefault("temporadas_completas", [])
            if temporada_antiga and temporada_antiga not in consumidor["temporadas_completas"]:
                consumidor["temporadas_completas"].append(temporada_antiga)
            aviso = f"Chopp resgatado! Nova cartela aberta — temporada {consumidor['temporada']}."
        else:
            # boas-vindas: NAO mexe na cartela nem gera punch
            aviso = "Chopp de boas-vindas resgatado! Agora é só juntar pontos."

        push = _notificar(consumidor, aviso)
        storage.salvar_evento({
            "id": str(uuid.uuid4()), "tipo": "resgate", "tipo_recompensa": tipo,
            "telefone": telefone, "bar": bar, "garcom_id": garcom_id, "data": agora(),
        })

        return jsonify({
            "status": "ok", "acao": "resgate", "tipo_resgatado": tipo,
            "nome": consumidor["nome"],
            "punches": consumidor["punches"], "meta": consumidor["meta"],
            "recompensas_disponiveis": len(_disponiveis(consumidor)),
            "mensagem_garcom": f"RESGATE — entregue 1 chopp grátis para {consumidor['nome']}.",
            "push": push,
        })

    # ─────────── CAMINHO 2: sem recompensa -> PONTUA ───────────
    meta = int(consumidor["meta"])
    consumidor["punches"] = int(consumidor.get("punches", 0)) + 1
    consumidor["ultimo_punch"] = agora()

    fechou = consumidor["punches"] >= meta

    if fechou:
        # a cartela NAO zera aqui. Ela trava cheia ate o resgate.
        consumidor["punches"] = meta
        consumidor["recompensas"].append({"tipo": "punch", "gerada_em": agora(), "resgatada": False})
        consumidor["recompensas_total"] = consumidor.get("recompensas_total", 0) + 1
        aviso = ("Cartela fechada! Seu próximo chopp é por nossa conta. "
                 "Chame o atendente para resgatar.")
    else:
        faltam = meta - consumidor["punches"]
        aviso = f"Chopp registrado! Faltam {faltam} para o seu chopp grátis."

    push = _notificar(consumidor, aviso)

    if fechou:
        _creditar_indicador(consumidor)

    storage.salvar_evento({
        "id": str(uuid.uuid4()), "tipo": "punch", "telefone": telefone, "bar": bar,
        "garcom_id": garcom_id, "recompensa_gerada": fechou, "data": agora(),
    })

    return jsonify({
        "status": "ok", "acao": "punch",
        "nome": consumidor["nome"],
        "punches": consumidor["punches"], "meta": meta,
        "recompensa_gerada": fechou,
        "recompensas_disponiveis": len(_disponiveis(consumidor)),
        "mensagem_garcom": (
            f"CARTELA FECHADA — {consumidor['nome']} tem 1 chopp grátis. Avise o cliente."
            if fechou else
            f"Ponto registrado — {consumidor['nome']}: {consumidor['punches']} de {meta}."
        ),
        "push": push,
    })


# rotas antigas continuam funcionando (o scanner atual ainda as usa)
@app.route("/punch", methods=["POST"])
def punch_legado():
    return scan()


@app.route("/resgatar", methods=["POST"])
def resgatar_legado():
    return scan()


# ══════════════════════════════════════════════════════════════
#  CARTAO .pkpass
# ══════════════════════════════════════════════════════════════

@app.route("/cartao/<telefone>")
def cartao(telefone):
    telefone = so_digitos(telefone)
    c = storage.carregar_consumidor(telefone)
    if not c:
        return jsonify({"erro": "consumidor_nao_encontrado"}), 404

    if not c.get("auth_token"):
        c["auth_token"] = secrets.token_hex(20)
        storage.salvar_consumidor(c)

    try:
        dados = pkpass.gerar_pkpass(c, aviso=c.get("aviso"))
    except Exception as e:
        app.logger.exception("falha ao gerar pkpass")
        return jsonify({"erro": "falha_ao_gerar_cartao", "detalhe": str(e)}), 500

    return Response(dados, mimetype="application/vnd.apple.pkpass", headers={
        "Content-Disposition": f'attachment; filename="clube-backbone-{telefone}.pkpass"',
        "Cache-Control": "no-store",
    })


# ══════════════════════════════════════════════════════════════
#  WEB SERVICE DA APPLE — as 4 rotas exigidas
#  Os caminhos e nomes sao definidos pela Apple. Nao mudar.
# ══════════════════════════════════════════════════════════════

def _autorizado(consumidor):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("ApplePass "):
        return False
    return auth[10:].strip() == consumidor.get("auth_token")


@app.route("/v1/devices/<device_id>/registrations/<pass_type_id>/<serial>", methods=["POST"])
def registrar_dispositivo(device_id, pass_type_id, serial):
    """O iPhone chama isto ao instalar o cartao, entregando o pushToken."""
    c = storage.carregar_consumidor(so_digitos(serial))
    if not c:
        return "", 404
    if not _autorizado(c):
        return "", 401

    push_token = (request.get_json(silent=True) or {}).get("pushToken")
    if not push_token:
        return "", 400

    registro = storage.carregar_registro(serial) or {}
    ja_existia = device_id in registro.get("dispositivos", {})

    storage.salvar_registro(serial, device_id, push_token)
    # 201 = registro novo | 200 = ja estava registrado
    return "", (200 if ja_existia else 201)


@app.route("/v1/devices/<device_id>/registrations/<pass_type_id>", methods=["GET"])
def listar_passes_atualizados(device_id, pass_type_id):
    """O iPhone pergunta: o que mudou desde X?"""
    desde = request.args.get("passesUpdatedSince")
    seriais = storage.seriais_do_dispositivo(device_id)
    if not seriais:
        return "", 204

    alterados, ultimo = [], desde or ""
    for s in seriais:
        c = storage.carregar_consumidor(s)
        if not c:
            continue
        atualizado = c.get("atualizado_em", "")
        if not desde or atualizado > desde:
            alterados.append(s)
        if atualizado > ultimo:
            ultimo = atualizado

    if not alterados:
        return "", 204

    return jsonify({"serialNumbers": alterados, "lastUpdated": ultimo})


@app.route("/v1/passes/<pass_type_id>/<serial>", methods=["GET"])
def baixar_pass_atualizado(pass_type_id, serial):
    """O iPhone acordou com o push e vem buscar o .pkpass novo."""
    c = storage.carregar_consumidor(so_digitos(serial))
    if not c:
        return "", 404
    if not _autorizado(c):
        return "", 401

    dados = pkpass.gerar_pkpass(c, aviso=c.get("aviso"))
    return Response(dados, mimetype="application/vnd.apple.pkpass", headers={
        "Last-Modified": c.get("atualizado_em", ""),
    })


@app.route("/v1/devices/<device_id>/registrations/<pass_type_id>/<serial>", methods=["DELETE"])
def remover_dispositivo(device_id, pass_type_id, serial):
    """O usuario apagou o cartao da Wallet."""
    c = storage.carregar_consumidor(so_digitos(serial))
    if not c:
        return "", 404
    if not _autorizado(c):
        return "", 401
    storage.remover_registro(device_id, serial)
    return "", 200


@app.route("/v1/log", methods=["POST"])
def log_apple():
    """A Apple manda os erros dela para ca. Otimo para depurar."""
    app.logger.warning("APPLE LOG: %s", json.dumps(request.get_json(silent=True) or {}))
    return "", 200


# ══════════════════════════════════════════════════════════════
#  ADMIN
# ══════════════════════════════════════════════════════════════

@app.route("/login", methods=["POST"])
def login():
    dados = request.get_json() or {}
    garcom = storage.carregar_garcom(dados.get("id"))
    if not garcom or garcom.get("senha") != dados.get("senha"):
        return jsonify({"erro": "credenciais_invalidas"}), 401
    if not garcom.get("ativo", True):
        return jsonify({"erro": "garcom_inativo"}), 403
    return jsonify({
        "status": "ok", "garcom_id": garcom["id"], "nome": garcom["nome"],
        "bar_id": garcom["bar_id"], "bar_nome": garcom.get("bar_nome", ""),
    })


@app.route("/admin/bares")
def admin_bares():
    return jsonify(storage.listar_bares())


@app.route("/admin/bares", methods=["POST"])
def admin_criar_bar():
    dados = request.get_json()
    dados.setdefault("id", str(uuid.uuid4())[:8])
    storage.salvar_bar(dados)
    return jsonify({"status": "ok", "bar": dados})


@app.route("/admin/garcons")
def admin_garcons():
    return jsonify(storage.listar_garcons())


@app.route("/admin/garcons", methods=["POST"])
def admin_criar_garcom():
    dados = request.get_json()
    dados.setdefault("id", str(uuid.uuid4())[:8])
    storage.salvar_garcom(dados)
    return jsonify({"status": "ok", "garcom": dados})


@app.route("/admin/consumidores")
def admin_consumidores():
    bar_filtro = request.args.get("bar")
    todos = storage.listar_consumidores()
    if bar_filtro:
        todos = [c for c in todos if c.get("bar_origem") == bar_filtro]
    for c in todos:
        c["recompensas_disponiveis"] = len(_disponiveis(c))
        c["total_indicados"] = len(c.get("indicados", []))
        c.pop("auth_token", None)   # nunca expor
    return jsonify(todos)


@app.route("/admin/config")
def admin_config():
    return jsonify(storage.carregar_config())


@app.route("/admin/config", methods=["POST"])
def admin_salvar_config():
    storage.salvar_config(request.get_json())
    return jsonify({"status": "ok"})


@app.route("/admin/push/<telefone>", methods=["POST"])
def admin_push_manual(telefone):
    """Dispara um push avulso. Base para campanhas segmentadas."""
    c = storage.carregar_consumidor(so_digitos(telefone))
    if not c:
        return jsonify({"erro": "nao_encontrado"}), 404
    texto = (request.get_json() or {}).get("texto", "")
    if not texto:
        return jsonify({"erro": "texto_vazio"}), 400
    return jsonify(_notificar(c, texto))


# ══════════════════════════════════════════════════════════════
#  EXTRAIR PADRINHO  (member gets member via BotConversa)
#  Recebe a mensagem suja e devolve so o telefone do padrinho.
# ══════════════════════════════════════════════════════════════

@app.route("/extrair-padrinho", methods=["POST", "GET"])
def extrair_padrinho():
    msg = ""
    if request.is_json:
        msg = (request.get_json(silent=True) or {}).get("mensagem", "")
    if not msg:
        msg = request.form.get("mensagem", "") or request.args.get("mensagem", "")

    padrinho = ""
    if msg:
        m = re.search(r"padrinho[^0-9]*([0-9][0-9\s\-\+\(\)]{9,})", msg, re.IGNORECASE)
        if m:
            padrinho = so_digitos(m.group(1))
        else:
            achados = re.findall(r"[0-9][0-9\s\-\+\(\)]{9,}", msg)
            cands = [so_digitos(a) for a in achados]
            cands = [c for c in cands if 10 <= len(c) <= 13]
            if cands:
                padrinho = max(cands, key=len)

    return jsonify({"padrinho": padrinho})


@app.route("/")
def health():
    return jsonify({"status": "ok", "sistema": "Clube Backbone", "versao": 3})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
