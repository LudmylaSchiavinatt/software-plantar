"""
build_exe.py - Gera executável PLANTAR com código-fonte encriptado (Fernet).

Uso:
    python build_exe.py

Resultado:
    dist/
        plantar.exe          <- executável standalone
        plantar.key          <- chave de distribuição usada no build

O executável abre o navegador automaticamente em http://127.0.0.1:5000
"""
import os
import sys
import glob
import shutil
import base64
import zipfile
import tempfile
from pathlib import Path

try:
    from cryptography.fernet import Fernet
except ImportError:
    print("[!] Instalando cryptography...")
    os.system(f"{sys.executable} -m pip install cryptography --quiet")
    from cryptography.fernet import Fernet

try:
    import PyInstaller
except ImportError:
    print("[!] Instalando PyInstaller...")
    os.system(f"{sys.executable} -m pip install pyinstaller --quiet")

# ============================================================
# Configuração
# ============================================================
ROOT = Path(__file__).parent
DIST = ROOT / "dist"
BUILD = ROOT / "build_temp"
ARQUIVO_CHAVE_DISTRIBUICAO = ROOT / "distribution.key"

# Arquivos Python que serão encriptados
PY_FILES = [
    "app.py",
    "processador_coaipro.py",
    "seed_loader.py",
]

# Pastas/arquivos de dados que vão junto (sem encriptar)
DATA_FILES = [
    "schema.sql",
    "requirements.txt",
]
DATA_DIRS = [
    "templates",
    "dados_seed",
    "planilhas_modelo",
]


def obter_chave_distribuicao() -> bytes:
    """
    Define a chave de distribuição nesta prioridade:
      1) variável de ambiente PLANTAR_DIST_KEY
      2) arquivo distribution.key na raiz do projeto
    """
    chave = None
    origem = None

    env_key = os.getenv("PLANTAR_DIST_KEY", "").strip()
    if env_key:
        chave = env_key.encode("utf-8")
        origem = "variável de ambiente PLANTAR_DIST_KEY"
    elif ARQUIVO_CHAVE_DISTRIBUICAO.exists():
        chave = ARQUIVO_CHAVE_DISTRIBUICAO.read_text(encoding="utf-8").strip().encode("utf-8")
        origem = f"arquivo {ARQUIVO_CHAVE_DISTRIBUICAO.name}"
    else:
        raise ValueError(
            "Chave de distribuição não informada. Defina PLANTAR_DIST_KEY OU crie "
            f"o arquivo {ARQUIVO_CHAVE_DISTRIBUICAO.name} com uma chave Fernet válida."
        )

    try:
        Fernet(chave)
    except Exception as exc:
        raise ValueError(
            "Chave de distribuição inválida. Forneça uma chave Fernet válida via "
            "PLANTAR_DIST_KEY ou no arquivo distribution.key."
        ) from exc

    print(f"  [+] Chave de distribuição carregada de: {origem}")
    return chave


def limpar():
    """Remove artefatos anteriores."""
    for d in [BUILD, DIST]:
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    BUILD.mkdir(parents=True, exist_ok=True)
    DIST.mkdir(parents=True, exist_ok=True)


def encriptar_fontes(chave: bytes) -> dict:
    """Encripta cada arquivo .py e retorna dict {nome: bytes_encriptados}."""
    f = Fernet(chave)
    pacote = {}
    for nome in PY_FILES:
        caminho = ROOT / nome
        if caminho.exists():
            conteudo = caminho.read_bytes()
            pacote[nome] = base64.b64encode(f.encrypt(conteudo)).decode('ascii')
            print(f"  [+] Encriptado: {nome} ({len(conteudo)} bytes)")
        else:
            print(f"  [!] Aviso: {nome} não encontrado, pulando")
    return pacote


def gerar_launcher(pacote: dict, chave: bytes):
    """Gera o launcher.py que desencripta e executa o app."""
    # Serializar pacote como dict literal
    pacote_str = "{\n"
    for k, v in pacote.items():
        pacote_str += f"    {repr(k)}: {repr(v)},\n"
    pacote_str += "}"

    launcher = f'''#!/usr/bin/env python3
"""PLANTAR - Launcher com código encriptado."""
import os
import sys
import base64
import tempfile
import shutil
import webbrowser
import threading
import time

# Chave embutida (ofuscada em base64)
_K = {repr(base64.b64encode(chave).decode('ascii'))}

# Pacote encriptado
_P = {pacote_str}


def main():
    from cryptography.fernet import Fernet

    chave = base64.b64decode(_K)
    f = Fernet(chave)

    # Criar diretório temporário para fontes desencriptados
    tmp = tempfile.mkdtemp(prefix="plantar_")

    try:
        # Desencriptar fontes
        for nome, enc_b64 in _P.items():
            enc = base64.b64decode(enc_b64)
            conteudo = f.decrypt(enc)
            destino = os.path.join(tmp, nome)
            with open(destino, "wb") as fp:
                fp.write(conteudo)

        # Copiar dados (schema, templates, etc)
        base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        for item in ["schema.sql", "requirements.txt"]:
            src = os.path.join(base_dir, item)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(tmp, item))

        for pasta in ["templates", "dados_seed", "planilhas_modelo"]:
            src = os.path.join(base_dir, pasta)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(tmp, pasta), dirs_exist_ok=True)

        # Adicionar tmp ao path e importar app
        sys.path.insert(0, tmp)
        os.chdir(tmp)

        # Abrir navegador após breve delay
        def abrir_nav():
            time.sleep(2)
            webbrowser.open("http://127.0.0.1:5000")

        threading.Thread(target=abrir_nav, daemon=True).start()

        print("=" * 50)
        print("  PLANTAR - Gestão de Contratos")
        print("  Agricultura Familiar")
        print("  http://127.0.0.1:5000")
        print("=" * 50)
        print()
        print("Pressione Ctrl+C para encerrar.")
        print()

        # Importar e rodar Flask
        import app as flask_app
        flask_app.app.run(host="127.0.0.1", port=5000, debug=False)

    except KeyboardInterrupt:
        print("\\nEncerrando PLANTAR...")
    finally:
        # Limpar fontes temporários
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
'''
    launcher_path = BUILD / "launcher.py"
    launcher_path.write_text(launcher, encoding="utf-8")
    print(f"  [+] Launcher gerado: {launcher_path}")
    return launcher_path


def copiar_dados():
    """Copia dados estáticos para o diretório de build."""
    for f in DATA_FILES:
        src = ROOT / f
        if src.exists():
            shutil.copy2(src, BUILD / f)

    for d in DATA_DIRS:
        src = ROOT / d
        if src.is_dir():
            shutil.copytree(src, BUILD / d, dirs_exist_ok=True)
    print("  [+] Dados estáticos copiados")


def gerar_spec(launcher_path: Path):
    """Gera o .spec para PyInstaller."""
    datas = []
    for f in DATA_FILES:
        if (BUILD / f).exists():
            p = str(BUILD / f).replace("\\", "/")
            datas.append(f'(r"{p}", ".")')
    for d in DATA_DIRS:
        if (BUILD / d).is_dir():
            p = str(BUILD / d).replace("\\", "/")
            datas.append(f'(r"{p}", "{d}")')

    datas_str = ",\n             ".join(datas)

    launcher_str = str(launcher_path).replace("\\", "/")
    build_str = str(BUILD).replace("\\", "/")

    spec = f'''# -*- mode: python ; coding: utf-8 -*-
a = Analysis(
    [r"{launcher_str}"],
    pathex=[r"{build_str}"],
    binaries=[],
    datas=[{datas_str}],
    hiddenimports=[
        "flask", "flask_cors", "jinja2", "markupsafe",
        "sqlite3", "pandas", "openpyxl", "cryptography",
        "cryptography.fernet", "werkzeug",
    ],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="plantar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    icon=None,
)
'''
    spec_path = BUILD / "plantar.spec"
    spec_path.write_text(spec, encoding="utf-8")
    print(f"  [+] Spec gerado: {spec_path}")
    return spec_path


def executar_pyinstaller(spec_path: Path):
    """Roda o PyInstaller."""
    cmd = (
        f'{sys.executable} -m PyInstaller '
        f'--distpath "{DIST}" '
        f'--workpath "{BUILD / "work"}" '
        f'"{spec_path}" '
        f'--noconfirm --clean'
    )
    print(f"\n  [>] Executando PyInstaller...")
    ret = os.system(cmd)
    if ret != 0:
        print(f"\n  [!] PyInstaller retornou código {ret}")
        return False
    return True


def main():
    print()
    print("=" * 55)
    print("  PLANTAR - Build Executável (Fernet Encrypted)")
    print("=" * 55)
    print()

    # 1. Limpar
    print("[1/6] Limpando artefatos...")
    limpar()

    # 2. Obter chave de distribuição (genérica/configurável)
    print("[2/6] Carregando chave de distribuição...")
    chave = obter_chave_distribuicao()
    chave_path = DIST / "plantar.key"
    chave_path.write_bytes(chave)
    print(f"  [+] Chave salva em: {chave_path}")

    # 3. Encriptar fontes
    print("[3/6] Encriptando código-fonte...")
    pacote = encriptar_fontes(chave)

    # 4. Gerar launcher
    print("[4/6] Gerando launcher...")
    launcher_path = gerar_launcher(pacote, chave)

    # 5. Copiar dados
    print("[5/6] Copiando dados estáticos...")
    copiar_dados()

    # 6. PyInstaller
    print("[6/6] Empacotando executável...")
    spec_path = gerar_spec(launcher_path)
    ok = executar_pyinstaller(spec_path)

    if ok and (DIST / "plantar.exe").exists():
        size_mb = (DIST / "plantar.exe").stat().st_size / (1024 * 1024)
        print()
        print("=" * 55)
        print(f"  BUILD CONCLUÍDO COM SUCESSO!")
        print(f"  Executável: dist/plantar.exe ({size_mb:.1f} MB)")
        print(f"  Chave:      dist/plantar.key")
        print("=" * 55)
        print()
        print("  Para executar:")
        print("    dist\\plantar.exe")
        print()
        print("  O navegador abrirá automaticamente em")
        print("  http://127.0.0.1:5000")
        print()
    else:
        print("\n  [!] Falha no build. Verifique os logs acima.\n")


if __name__ == "__main__":
    main()
