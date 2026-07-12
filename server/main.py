# Backbone Beer — Sistema de Fidelização de Consumidores.

import os
import json
import uuid
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from flask_cors import CORS
import storage

app = Flask(__name__)
CORS(app)

# ── Cadastro de consumidor ────────────────────────────────────

@app.route("/cadastro/<bar>/<telefone>/<nome>/<dia>/<mes>/<ano>/<time>")
def cadastro(bar, telefone, nome, dia, mes, ano, time):
    """
    Recebe os dados do consumidor via URL gerada pelo BotConversa.
    Cria o perfil se não existir. Retorna status do cadastro.
    """
    from urllib.parse import unquote
    nome = unquote(nome)
    bar  = unquote(bar)
    time = unquote(time)

    # Verifica se já existe
    existente = storage.carregar_consumidor(telefone)
    if existente:
        return jsonify({"status": "ja_cadastrado", "consumidor": existente})

    # Cria perfil novo
    consumidor = {
        "telefone":      telefone,
        "nome":          nome,
        "bar_origem":    bar,
        "nascimento_dia": dia,
        "nascimento_mes": mes,
        "nascimento_ano": ano,
        "time":          time,
        "punches":       0,
        "recompensas_disponiveis": 1,   # chopp grátis de boas-vindas
        "recompensas_total": 0,
        "ultimo_punch":  None,
        "cadastro_em":   datetime.now(timezone.utc).isoformat(),
        "ativo":         True
    }

    storage.salvar_consumidor(consumidor)

    # Registra evento de cadastro
    evento = {
        "id":        str(uuid.uuid4()),
        "tipo":      "cadastro",
        "telefone":  telefone,
        "bar":       bar,
        "garcom_id": None,
        "data":      datetime.now(timezone.utc).isoformat()
    }
    storage.salvar_evento(evento)

    return jsonify({"status": "cadastrado", "consumidor": consumidor})

# ── Consulta de consumidor ────────────────────────────────────

@app.route("/consumidor/<telefone>")
def consultar_consumidor(telefone):
    """Retorna o perfil completo de um consumidor."""
    c = storage.carregar_consumidor(telefone)
    if not c:
        return jsonify({"erro": "nao_encontrado"}), 404
    config = storage.carregar_config()
    c["punches_para_recompensa"] = config["punches_para_recompensa"]
    return jsonify(c)

# ── Punch (garçom escaneia cartão do cliente) ─────────────────

@app.route("/punch", methods=["POST"])
def punch():
    """
    Registra um punch.
    Body JSON: { telefone, garcom_id, bar }
    """
    dados = request.get_json()
    telefone  = dados.get("telefone")
    garcom_id = dados.get("garcom_id")
    bar       = dados.get("bar")

    if not telefone or not garcom_id or not bar:
        return jsonify({"erro": "dados_incompletos"}), 400

    consumidor = storage.carregar_consumidor(telefone)
    if not consumidor:
        return jsonify({"erro": "consumidor_nao_encontrado"}), 404

    garcom = storage.carregar_garcom(garcom_id)
    if not garcom:
        return jsonify({"erro": "garcom_nao_encontrado"}), 404

    # Garçom só pode pontuar consumidores do seu bar
    if garcom.get("bar_id") != bar:
        return jsonify({"erro": "bar_diferente"}), 403

    config = storage.carregar_config()
    punches_para_recompensa = config.get("punches_para_recompensa", 10)

    # Incrementa punches
    consumidor["punches"] = consumidor.get("punches", 0) + 1
    consumidor["ultimo_punch"] = datetime.now(timezone.utc).isoformat()

    # Verifica se atingiu recompensa
    recompensa_gerada = False
    if consumidor["punches"] >= punches_para_recompensa:
        consumidor["punches"] = 0
        consumidor["recompensas_disponiveis"] = consumidor.get("recompensas_disponiveis", 0) + 1
        consumidor["recompensas_total"] = consumidor.get("recompensas_total", 0) + 1
        recompensa_gerada = True

    storage.salvar_consumidor(consumidor)

    # Registra evento
    evento = {
        "id":               str(uuid.uuid4()),
        "tipo":             "punch",
        "telefone":         telefone,
        "bar":              bar,
        "garcom_id":        garcom_id,
        "recompensa_gerada": recompensa_gerada,
        "data":             datetime.now(timezone.utc).isoformat()
    }
    storage.salvar_evento(evento)

    return jsonify({
        "status":            "ok",
        "punches":           consumidor["punches"],
        "punches_para_recompensa": punches_para_recompensa,
        "recompensas_disponiveis": consumidor["recompensas_disponiveis"],
        "recompensa_gerada": recompensa_gerada,
        "nome":              consumidor["nome"]
    })

# ── Resgate de recompensa ─────────────────────────────────────

@app.route("/resgatar", methods=["POST"])
def resgatar():
    """
    Registra o resgate de um chopp grátis.
    Body JSON: { telefone, garcom_id, bar }
    """
    dados = request.get_json()
    telefone  = dados.get("telefone")
    garcom_id = dados.get("garcom_id")
    bar       = dados.get("bar")

    consumidor = storage.carregar_consumidor(telefone)
    if not consumidor:
        return jsonify({"erro": "consumidor_nao_encontrado"}), 404

    if consumidor.get("recompensas_disponiveis", 0) < 1:
        return jsonify({"erro": "sem_recompensa"}), 400

    config = storage.carregar_config()
    delay_horas = config.get("delay_resgate_horas", 24)

    # Verifica delay mínimo desde último punch
    ultimo_punch = consumidor.get("ultimo_punch")
    if ultimo_punch:
        ultimo = datetime.fromisoformat(ultimo_punch)
        agora  = datetime.now(timezone.utc)
        horas_passadas = (agora - ultimo).total_seconds() / 3600
        if horas_passadas < delay_horas:
            horas_faltam = round(delay_horas - horas_passadas, 1)
            return jsonify({
                "erro": "delay_nao_atingido",
                "horas_faltam": horas_faltam
            }), 400

    consumidor["recompensas_disponiveis"] -= 1
    storage.salvar_consumidor(consumidor)

    evento = {
        "id":        str(uuid.uuid4()),
        "tipo":      "resgate",
        "telefone":  telefone,
        "bar":       bar,
        "garcom_id": garcom_id,
        "data":      datetime.now(timezone.utc).isoformat()
    }
    storage.salvar_evento(evento)

    return jsonify({
        "status": "ok",
        "recompensas_disponiveis": consumidor["recompensas_disponiveis"],
        "nome": consumidor["nome"]
    })

# ── Login do garçom ───────────────────────────────────────────

@app.route("/login", methods=["POST"])
def login():
    """
    Login do garçom.
    Body JSON: { id, senha }
    """
    dados = request.get_json()
    garcom_id = dados.get("id")
    senha     = dados.get("senha")

    garcom = storage.carregar_garcom(garcom_id)
    if not garcom or garcom.get("senha") != senha:
        return jsonify({"erro": "credenciais_invalidas"}), 401

    if not garcom.get("ativo", True):
        return jsonify({"erro": "garcom_inativo"}), 403

    return jsonify({
        "status":   "ok",
        "garcom_id": garcom_id,
        "nome":     garcom["nome"],
        "bar_id":   garcom["bar_id"],
        "bar_nome": garcom.get("bar_nome", "")
    })

# ── Admin — Bares ─────────────────────────────────────────────

@app.route("/admin/bares")
def admin_bares():
    return jsonify(storage.listar_bares())

@app.route("/admin/bares", methods=["POST"])
def admin_criar_bar():
    dados = request.get_json()
    if not dados.get("id"):
        dados["id"] = str(uuid.uuid4())[:8]
    storage.salvar_bar(dados)
    return jsonify({"status": "ok", "bar": dados})

# ── Admin — Garçons ───────────────────────────────────────────

@app.route("/admin/garcons")
def admin_garcons():
    return jsonify(storage.listar_garcons())

@app.route("/admin/garcons", methods=["POST"])
def admin_criar_garcom():
    dados = request.get_json()
    if not dados.get("id"):
        dados["id"] = str(uuid.uuid4())[:8]
    storage.salvar_garcom(dados)
    return jsonify({"status": "ok", "garcom": dados})

# ── Admin — Consumidores ──────────────────────────────────────

@app.route("/admin/consumidores")
def admin_consumidores():
    bar_filtro = request.args.get("bar")
    todos = storage.listar_consumidores()
    if bar_filtro:
        todos = [c for c in todos if c.get("bar_origem") == bar_filtro]
    return jsonify(todos)

# ── Admin — Config ────────────────────────────────────────────

@app.route("/admin/config")
def admin_config():
    return jsonify(storage.carregar_config())

@app.route("/admin/config", methods=["POST"])
def admin_salvar_config():
    dados = request.get_json()
    storage.salvar_config(dados)
    return jsonify({"status": "ok"})

# ── Health check ──────────────────────────────────────────────

@app.route("/")
def health():
    return jsonify({"status": "ok", "sistema": "Backbone Beer Fidelidade"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
