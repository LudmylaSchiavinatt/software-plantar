# -*- coding: utf-8 -*-
"""
debug_agrolink_busca.py

Confirma se a página de busca por Ceasa (CEASA-SP Campinas) do Agrolink
retorna o preço em TEXTO puro (e não como sprite/imagem, que é o que
acontece nas páginas de produto individuais tipo /cotacoes/frutas/goiaba/).

Uso:
    python debug_agrolink_busca.py
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

print(f"Buscando: {URL}\n")
resp = requests.get(URL, headers=HEADERS, timeout=15)
print(f"Status HTTP: {resp.status_code}")
print(f"Tamanho da resposta: {len(resp.text)} caracteres\n")

soup = BeautifulSoup(resp.text, "html.parser")
tabela = soup.find("table")

if tabela is None:
    print("Nenhuma tabela encontrada nesta URL.")
else:
    linhas = tabela.find_all("tr")
    print(f"Número de <tr> na tabela: {len(linhas)}\n")
    print("=== PRIMEIRAS 15 LINHAS (produto | ceasa | preço texto | data) ===")
    for i, tr in enumerate(linhas[:15]):
        celulas = [td.get_text(strip=True) for td in tr.find_all("td")]
        print(f"  linha {i}: {celulas}")

        # Se alguma célula parecer ser a de preço, mostra o HTML bruto dela
        # pra confirmar se é texto mesmo ou se tem <div style="background-image...">
        for td in tr.find_all("td"):
            if "background-image" in str(td):
                print(f"    -> ATENÇÃO: célula com sprite escondido: {str(td)[:200]}")

print("\nSe as linhas acima mostrarem valores tipo 'R$ 6,33' como texto normal")
print("(sem nenhum aviso de sprite escondido), a fonte está confiável por ora.")