# -*- coding: utf-8 -*-
"""
cotacoes_service.py

Busca cotações REAIS de mercado para uso no template Jinja da PLANTAR.

Fonte:
  - Goiaba, Caqui,
    Abacate, Atemoia,
    Pitaya               -> Agregador Agrolink, que replica os boletins da
                             CEAGESP (a consulta oficial da CEAGESP é um
                             formulário dinâmico em JS, sem endpoint público
                             estável, então usamos o agregador que já
                             raspa e publica esses dados em HTML estático).

Uso no Flask:

    from cotacoes_service import get_todas_cotacoes

    @app.route("/cooperativa")
    def cooperativa():
        cotacoes = get_todas_cotacoes()   # já vem com cache de 1h
        return render_template("cooperativa_visao_cooperado.html", cotacoes=cotacoes, ...)

Cada valor do dicionário de retorno tem o formato:
    {"preco": 7.89, "tendencia": "alta", "variacao": "+0,61%", "data": "24/07/2026"}
ou None se não foi possível obter a cotação (o template já trata isso com `| default(0)`).
"""

import re
import time
import logging
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("cotacoes_service")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
TIMEOUT = 10  # segundos

# ---------------------------------------------------------------------------
# Cache simples em memória (evita bater nos sites a cada request de usuário).
# Essas cotações mudam no máximo 1x por dia útil, então um TTL de 1h é
# mais que suficiente e educado com os servidores de origem.
# ---------------------------------------------------------------------------
_CACHE = {}
_CACHE_TTL_SECONDS = 60 * 60  # 1 hora


def _cache_get(key):
    item = _CACHE.get(key)
    if not item:
        return None
    value, expires_at = item
    if time.time() > expires_at:
        return None
    return value


def _cache_set(key, value):
    _CACHE[key] = (value, time.time() + _CACHE_TTL_SECONDS)


def _parse_valor_br(texto):
    """Converte '1.753,28' -> 1753.28 (float)."""
    texto = texto.strip().replace(".", "").replace(",", ".")
    return float(texto)


def _classificar_tendencia(variacao_pct):
    return "alta" if variacao_pct >= 0 else "baixa"


def _formatar_variacao(variacao_pct):
    sinal = "+" if variacao_pct >= 0 else ""
    return f"{sinal}{variacao_pct:.2f}%".replace(".", ",")


# ---------------------------------------------------------------------------
# CEAGESP (via agregador Agrolink) — Goiaba, Caqui, Abacate, Atemoia, Pitaya
# ---------------------------------------------------------------------------

# slug usado na URL da Agrolink para cada fruta
AGROLINK_SLUGS = {
    "goiaba": "goiaba",
    "caqui": "caqui",
    "abacate": "abacate",
    "atemoia": "atemoia",   # também pode aparecer grafado "atemoia"/"atemóia"
    "pitaya": "pitaya",     # também pode aparecer grafado "pitaia"
}

AGROLINK_URL_TEMPLATE = "https://www.agrolink.com.br/cotacoes/frutas/{slug}/"


def fetch_ceagesp_fruta(produto):
    """
    Busca a cotação mais recente de origem CEAGESP para uma fruta,
    via agregador Agrolink.

    Retorna dict {"preco", "tendencia", "variacao", "data"} ou None se
    a fruta não tiver cotação disponível (produto sem indicador diário,
    fora de época, ou fonte fora do ar).
    """
    cache_key = f"ceagesp:{produto}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    slug = AGROLINK_SLUGS.get(produto)
    if not slug:
        raise ValueError(f"Produto CEAGESP desconhecido: {produto}")

    url = AGROLINK_URL_TEMPLATE.format(slug=slug)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Falha ao buscar CEAGESP/Agrolink (%s): %s", produto, exc)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    tabela = soup.find("table")
    if tabela is None:
        logger.warning("Tabela de cotações não encontrada para %s", produto)
        return None

    # Colunas: Produto | Ceasa | Preço | Data
    registros = []
    for tr in tabela.find_all("tr"):
        celulas = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(celulas) < 4:
            continue
        _produto_txt, ceasa_txt, preco_txt, data_txt = celulas[:4]
        if "CEAGESP" not in ceasa_txt.upper():
            continue
        if not re.match(r"\d{2}/\d{2}/\d{4}", data_txt):
            continue
        preco_match = re.search(r"[\d.,]+", preco_txt)
        if not preco_match:
            continue
        try:
            preco = _parse_valor_br(preco_match.group())
        except ValueError:
            continue
        try:
            data = datetime.strptime(data_txt, "%d/%m/%Y")
        except ValueError:
            continue
        registros.append({"preco": preco, "data": data, "data_str": data_txt, "ceasa": ceasa_txt})

    if not registros:
        logger.info("Sem cotação CEAGESP disponível para %s no momento", produto)
        return None

    # Ordena por data desc; usamos a unidade CEAGESP mais recente disponível
    registros.sort(key=lambda r: r["data"], reverse=True)
    mais_recente = registros[0]

    # Para calcular variação, procura o registro anterior da MESMA unidade CEAGESP
    anteriores_mesma_unidade = [
        r for r in registros[1:]
        if r["ceasa"] == mais_recente["ceasa"] and r["data"] < mais_recente["data"]
    ]

    variacao_pct = 0.0
    if anteriores_mesma_unidade:
        anterior = anteriores_mesma_unidade[0]
        if anterior["preco"] > 0:
            variacao_pct = ((mais_recente["preco"] - anterior["preco"]) / anterior["preco"]) * 100

    resultado = {
        "preco": mais_recente["preco"],
        "tendencia": _classificar_tendencia(variacao_pct),
        "variacao": _formatar_variacao(variacao_pct),
        "data": mais_recente["data_str"],
    }
    _cache_set(cache_key, resultado)
    return resultado


# ---------------------------------------------------------------------------
# Função principal usada pela rota Flask
# ---------------------------------------------------------------------------

def get_todas_cotacoes():
    """
    Monta o dicionário completo de cotações para passar ao template Jinja.
    Produtos que falharem na busca voltam como None (o template já trata
    isso com `| default(0)` e não quebra a página).
    """
    cotacoes = {}

    for produto in AGROLINK_SLUGS:
        try:
            cotacoes[produto] = fetch_ceagesp_fruta(produto)
        except Exception:
            logger.exception("Erro inesperado buscando %s", produto)
            cotacoes[produto] = None

    return cotacoes


if __name__ == "__main__":
    # Teste manual: roda `python cotacoes_service.py` pra ver o resultado no terminal
    logging.basicConfig(level=logging.INFO)
    import json

    resultado = get_todas_cotacoes()
    print(json.dumps(resultado, indent=2, ensure_ascii=False))
