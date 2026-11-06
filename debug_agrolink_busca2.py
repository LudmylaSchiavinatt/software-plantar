# -*- coding: utf-8 -*-
"""
debug_agrolink_busca2.py

Mostra o HTML bruto completo das primeiras linhas da tabela de busca por
Ceasa (CEASA-SP Campinas) do Agrolink, pra descobrir exatamente em qual
tag/estrutura está o nome do produto (já que <td> sozinho não pegou).

Uso:
    python debug_agrolink_busca2.py
"""

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

URL = "https://www.agrolink.com.br/cotacoes/ceasa/ceasa---sp/frutas/politi"

resp = requests.get(URL, headers=HEADERS, timeout=15)
soup = BeautifulSoup(resp.text, "html.parser")
tabela = soup.find("table")
linhas = tabela.find_all("tr")

print(f"Total de <tr>: {len(linhas)}\n")
print("=== HTML BRUTO DAS PRIMEIRAS 4 LINHAS (tr completo) ===\n")
for i, tr in enumerate(linhas[:4]):
    print(f"--- linha {i} ---")
    print(str(tr))
    print()