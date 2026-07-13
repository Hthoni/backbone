# Backbone Beer - Sistema de Fidelizacao de Consumidores.

import os
import json
import uuid
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from flask_cors import CORS
import storage

app = Flask(__name__)
CORS(app)

def _count_disponiveis(consumidor):
    return len([r for r in consumidor.get("recompensas", []) if not r.get("resgatada")])

def _garcom_pertence_bar(garcom, bar):
    return garcom.get("bar_id") == bar

@app.route("/cadastro/<bar>/<telefone>/<nome>/<dia>/<mes>/<time>")
def cadastro(bar, telefone, nome, dia, mes, time):
    from urllib.parse import unquote
    nome = unquote(nome)
    bar  = unquote(bar)
    time = unquote(time)

    existente = storage.carregar_consumidor(telefone)
    if existente:
        return jsonify({"status": "ja_cadastrado", "consumidor": existente})

    consumidor = {
        "telefone":      telefone,
        "nome":          nome,
        "bar_origem":    bar,
        "nascimento_dia": dia,
        "nascimento_mes": mes,
        "time":          time,
        "punches":       0,
        "recompensas": [
            {"tipo": "boas_vindas", "gerada_em": datetime.now(timezone.utc).isoformat(), "resgatada": False}
        ],
        "recompensas_total": 0,
        "ultimo_punch":  None,
        "cadastro_em":   datetime.now(timezone.utc).isoformat(),
        "ativo":         True
    }

    storage.salvar_consumidor(consumidor)

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

@app.route("/consumidor/<telefone>")
def consultar_consumidor(telefone):
    c = storage.carregar_consumidor(telefone)
    if not c:
        return jsonify({"erro": "nao_encontrado"}), 404
    config = storage.carregar_config()
    c["punches_para_recompensa"] = config["punches_para_recompensa"]
    c["recompensas_disponiveis"] = _count_disponiveis(c)
    return jsonify(c)

@app.route("/punch", methods=["POST"])
def punch():
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

    if not _garcom_pertence_bar(garcom, bar):
        return jsonify({"erro": "bar_diferente"}), 403

    config = storage.carregar_config()
    punches_para_recompensa = config.get("punches_para_recompensa", 10)

    if "recompensas" not in consumidor:
        consumidor["recompensas"] = []

    consumidor["punches"] = consumidor.get("punches", 0) + 1
    consumidor["ultimo_punch"] = datetime.now(timezone.utc).isoformat()

    recompensa_gerada = False
    if consumidor["punches"] >= punches_para_recompensa:
        consumidor["punches"] = 0
        consumidor["recompensas"].append({
            "tipo": "punch",
            "gerada_em": datetime.now(timezone.utc).isoformat(),
            "resgatada": False
        })
        consumidor["recompensas_total"] = consumidor.get("recompensas_total", 0) + 1
        recompensa_gerada = True

    storage.salvar_consumidor(consumidor)

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
        "recompensas_disponiveis": _count_disponiveis(consumidor),
        "recompensa_gerada": recompensa_gerada,
        "nome":              consumidor["nome"]
    })

@app.route("/resgatar", methods=["POST"])
def resgatar():
    dados = request.get_json()
    telefone  = dados.get("telefone")
    garcom_id = dados.get("garcom_id")
    bar       = dados.get("bar")

    consumidor = storage.carregar_consumidor(telefone)
    if not consumidor:
        return jsonify({"erro": "consumidor_nao_encontrado"}), 404

    disponiveis = [r for r in consumidor.get("recompensas", []) if not r.get("resgatada")]
    if not disponiveis:
        return jsonify({"erro": "sem_recompensa"}), 400

    # Prioriza boas_vindas (sem delay), depois punch (com delay)
    disponiveis.sort(key=lambda r: 0 if r.get("tipo") == "boas_vindas" else 1)
    recompensa = disponiveis[0]

    if recompensa.get("tipo") == "punch":
        config = storage.carregar_config()
        delay_horas = config.get("delay_resgate_horas", 24)

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

    for r in consumidor["recompensas"]:
        if r is recompensa:
            r["resgatada"] = True
            r["resgatada_em"] = datetime.now(timezone.utc).isoformat()
            r["resgatada_bar"] = bar
            r["resgatada_garcom"] = garcom_id
            break

    storage.salvar_consumidor(consumidor)

    evento = {
        "id":              str(uuid.uuid4()),
        "tipo":            "resgate",
        "tipo_recompensa": recompensa.get("tipo"),
        "telefone":        telefone,
        "bar":             bar,
        "garcom_id":       garcom_id,
        "data":            datetime.now(timezone.utc).isoformat()
    }
    storage.salvar_evento(evento)

    return jsonify({
        "status": "ok",
        "recompensas_disponiveis": _count_disponiveis(consumidor),
        "tipo_resgatado": recompensa.get("tipo"),
        "nome": consumidor["nome"]
    })

@app.route("/login", methods=["POST"])
def login():
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

@app.route("/admin/consumidores")
def admin_consumidores():
    bar_filtro = request.args.get("bar")
    todos = storage.listar_consumidores()
    if bar_filtro:
        todos = [c for c in todos if c.get("bar_origem") == bar_filtro]
    for c in todos:
        c["recompensas_disponiveis"] = _count_disponiveis(c)
    return jsonify(todos)

@app.route("/admin/config")
def admin_config():
    return jsonify(storage.carregar_config())

@app.route("/admin/config", methods=["POST"])
def admin_salvar_config():
    dados = request.get_json()
    storage.salvar_config(dados)
    return jsonify({"status": "ok"})

@app.route("/")
def health():
    return jsonify({"status": "ok", "sistema": "Backbone Beer Fidelidade"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
