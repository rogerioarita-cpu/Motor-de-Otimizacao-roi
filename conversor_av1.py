#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Compactador de Vídeos (C&C) - varre uma pasta e reduz todos os vídeos convertendo
para AV1 (SVT-AV1), mantendo o arquivo original e o reduzido.
Também procura vídeos grandes no computador (botão "Procurar vídeos grandes no PC").

Uso:
    python conversor_av1.py                      -> abre a janela
    python conversor_av1.py --cli "C:\\Videos"    -> roda no terminal (bom para agendar)

Requisitos: Python 3.9+ e ffmpeg/ffprobe no PATH (com o codificador libsvtav1).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

TITULO = "Compactador de Vídeos"
VERSAO = "1.1.0"          # ao mudar, o GitHub Actions gera e publica o instalador novo
EXTENSOES = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".wmv", ".flv", ".webm",
             ".mpg", ".mpeg", ".ts", ".mts", ".m2ts", ".3gp"}
FAMILIA_MP4 = {".mp4", ".mov", ".m4v", ".3gp"}
SUFIXO = " - RED"
SUFIXOS_DE_SAIDA = (SUFIXO, " - AV1")     # " - AV1" = saídas da versão anterior
MARCA_PARCIAL = ".parcial"
NOME_LOG = "compactacao_log.csv"
CRF_PADRAO = 35
PRESET_PADRAO = 6
MARCADO, DESMARCADO = "\u2611", "\u2610"     # caixinhas da coluna de seleção da lista
SEM_JANELA = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
PASTA_APP = Path(__file__).resolve().parent


def _achar(nome: str) -> str | None:
    """Procura a ferramenta primeiro junto do aplicativo (instalação) e depois no PATH."""
    exe = nome + (".exe" if os.name == "nt" else "")
    for base in (PASTA_APP / "ffmpeg", PASTA_APP):
        if (base / exe).is_file():
            return str(base / exe)
    return shutil.which(nome)


FFMPEG = _achar("ffmpeg") or "ffmpeg"
FFPROBE = _achar("ffprobe") or "ffprobe"
_VMAF: dict = {"exe": False}     # False = ainda não verificado


def ferramenta_vmaf() -> str | None:
    """Executável capaz de medir VMAF (filtro libvmaf), ou None se não houver."""
    if _VMAF["exe"] is False:
        achado = None
        for cand in (_achar("ffvmaf"), _achar("ffmpeg")):
            if not cand:
                continue
            try:
                saida = subprocess.run([cand, "-hide_banner", "-filters"], capture_output=True,
                                       text=True, errors="replace", creationflags=SEM_JANELA).stdout
            except OSError:
                continue
            if " libvmaf " in saida:
                achado = cand
                break
        _VMAF["exe"] = achado
    return _VMAF["exe"]


def _ocultar(caminho: Path, oculto: bool = True) -> None:
    """No Windows, marca/desmarca o arquivo como oculto (usado no temporário da conversão)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(str(caminho), 0x02 if oculto else 0x80)
    except Exception:
        pass

# Padrão visual C&C (derivado do logo)
AZUL = "#0446A4"
AZUL_ESCURO = "#03357D"
AZUL_CLARO = "#DCE7F7"
FUNDO = "#F5F7FB"
BRANCO = "#FFFFFF"
TEXTO = "#1F2A3D"
CINZA = "#66728A"
LOGO_B64 = "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"
ICONE_B64 = "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAJSklEQVR42u2aeXBX1RXHz7n3/d4vvyX5ZcMkkI0kYIIkJCKLAUxEBzQqdqxDK45VsS4ztFOdalXaYmyLYF1mqg7ihkUpUEQrbqgYwhKIZckCIaySPfllJ/ntv/fuPf3jGRuQQHD8x5l3/vq9eTP33M+553zvuff9kIjgp2wMfuJmApgAJoAJYAKYACaACWACmAAmgAlgApgAP9CU4Q/fHfCJiCECoi+of1LZ9vmB9l2HOlu6fY4IxW7lNiuPi4ooyktcfGNmdkoUACxdUzv7iviSGeN0ITnDS5qBkKRwBgCt3f4tlW3l1R176nv6PKHICG63Kg6bkpbgvH1O6sLitCi75cCJ3ne2Nby05KrzAxgmiThjkuiNT06+8uGxutNnwBO8ZnZq6a/y8jJinDalbzBUdarv3+WNqz48uqAwJX9C3CsfHJs64eofED9j9r2Dob9vOLKurKG9zQMK3jE/c2FR2sTkSM6wucu3o7Zzxfq6ZW/XPrhg4u7DXUKefY1Cw0xKqQtBRB29/hse2waFb0PxWnXeutc/Pk7ns/d3NTlK1sP0t6Bo7Ybtp4lI04UctWm6IKIdtZ3jF/0HCtfAnH+m3/H+zlr39x15/NpvX94H096Eq9dc+/ttw18pZ8ceOGMdvf6Sx8tq6rttMbZQQFv9yIx7b8jShSQCxgABiEASIMJtc1Jz0lwlT5Q1NQ34QwIARn/JZMT+iwPtt/15pz+oW51qlF39aPnc3PHRmi4QEdGILxCB06a89JtpqZc5Hntpny+oXaCIKazJO1fsqanvccTZA2eCi+Zl3HtDVlgXDFHhyBARkTFUOHKGYU3kpLpWPzKThPQE9NFnjiTinB1tGlj01wp/SI9wWEJ+beWDBbnjo8OaUDjjDBkiQ+QMFY5CSl3IRxdOmjc7ta0nMCIAZ+zVj0+U72mxRkcEgrrNZV26KJcI+FA8zjGLwnRdzp82tmh2anuP79xZShKShDzv3R9KIX/36sG+3oDdbgl6w/mTx9x1fcZ3BX2uVg65f+quXMZGltGegdALm+vRpiCADGjFBYk5qS4iyS4oLETw6/mZvqAwnobHWOFM4YwxlMMghCDOcOv+jm2VrWqkKoUEXS66Nt2iMCkJR3DFGRLRzJz43IzYEWV0886mljaPYlMkERAU511GAEKCwkecPecIQLcUJl91eRwR8aH4cMYOnujdfbgrIcY2f/rYWKcqiYy5Gav59uffoCSGoAliNn5dfiIB4MW2JUR4/v6CEVdg64F2JGIAkghUnpsRgwA4Cll3OdTsVJfhwCjxJf/Y98tnKroHQo+/UXXl4o+rT/YhgCQiIs6xZzC461AnWbkgErpMirOnJzkRgMHFneWkuUYEqGs4QxYuCUgSMEyJdwDAaLYlKUlIaWgLY7j6oxOr1lStvC9/+eL8DX+c3dTU//S6w0bgjVQ61uzp9YQ4QyQAQYmxNmeEMmr5kiMCuPsDRroTgEXBCJXB6AgQv60zo1g27W7mNhUAw7rMz4yxxjv6PKHvCgYAOnr9pEvGERBAksPKVYUR0Wh8sbNT4iwAIQgQAAEBJYG8xC8HNJRDuiAh6d2vGlSFbatyh3r9SxZMFJJ0/dtXmpDDRycE+lGauRinSpKQABkKQQNebZiujGIdAIQkQZCfEc2svKzaXbq29ul3Dr2ydPYvitM5Q6vKI6ycM4yyq2CEHBAYegMirEtE+AEcZ2Ve1rgod7cPFIYAoMtGt2dadtwljYkAnGHRlMtWbaoLhPTnNh+tXX1T1rjIUFi89unJEy2DIU3kZcZekztGVbkmCBkAZ+7+gDegWy1WADm6ohthBWZNHgNCIgBDAk3UnO4f3qJeJHkAiEDh7PmN9S+8d/SqvASpCS2ob6lsM9T2phnjXt96au/R7nlTk7JTXBmJDggLAOIKc/cGG93eS+pEzg+waG66PSpCCEkEoPDy2i4piXN24ZUlAoYoJTGGS17e/9iLe5ffl//esiKbVdEJlr5Ztbe+R+Es3mXNTonc8pdrL0+Jsqr81lmpFNaNroSC+vaaTqMZu0ikCPACRZyXEXPH3HTh0xhnil35b11XRV0XQxQjD6wLYgzfr2jeXtNZXuNetbZ2cn5iUW5CeqJz2T1TKCx0SXev3OsN6i9uri+Znpw1NjKkCSJ46OYJrjEOoQlAAAU37mjSdcnYhRbBiNGTb1WPCCCJnr57SnKKK+gLqxYmhXzizRpNl5yfh4EAdCEtCvMF9UdXHegeCH62v4Mh+oK6L6Dpgh69fdL8WSnSr53q8Mx9+MtGt/dPd04O68aJh9ITnaV3T9G9YSSw2i1Vdd2vb/2GM2a0vd8PvKZLztln+9pXbqgbEYAkjYu3b1w2J9qp+gdDjihrZW3nAy9+LQkUzoT8VgR1QbogBLAo3OPXblm6vbFlsCAr1uvXIUJpaPWs39GscOQc3/lDYcEV8REc9x/pGvQLi8JVhSmceYO6kPLh27Lvu31SsD8IBKpdeeK1qi8PdqgWjgi6kGLIl7E5qha+u67r509+NSM7/qxeprS0dPh+JCSlJzivm5q053B3e6uHOSzVx3r3n+zPHR+TFGfjDBlDzpAzBMRPKlsXPFnW0uHd/Mzcwklj+r3hD8qbVKel4lDn2Dj7xOQoh01p6wmUVzRzl+1ow5lddV1j4+wbyxuPNw/MzBmjC7ng6mSPLiqq3UKQANiyp9VpVwoyY1QLZ0O+GMN+b3jFv+ru/duuW+akbXrqGpv1/+KJw0XG+G30tH2e0LMbjqz54pueTh/4NXBZbyxMKZwUnxATMejTjrV4yqrdTR2ehcVpzz40NTXeLqSUEu55rnL9luPAEDQRmxzFASZlRC+en1n67uGGU2dASNDEzDkpny2f63JaiAgBGcNPv25dvu5wZX03+HXQZUZO/M9mpWSnRimMtfT4q072lVe7nSp/6p4pD9w84Vzh/j6AUS6cMwDo6g+W17i/PtZz6HT/kcYBjz9styqJsbaMsZHFUxJuLUzOSIoEACEkIholuGlHU1m12xPQoh3q9Vcm3TorWeGsoy+w9svTx1sGCybE3n9jls3Kjc6ZAGjI1566rp21XQdP9dWe6mvq8nLO4iKtqQnOyenRJTOSSqaPs1q4lBIA2LAzAZp/NTABTAATwAQwAUwAE8AEMAFMABPABDABTICfpv0PaZASbFDvN9YAAAAASUVORK5CYII="


# --------------------------------------------------------------------------
# Motor de conversão (independente da interface)
# --------------------------------------------------------------------------
@dataclass
class Tarefa:
    origem: Path
    destino: Path


@dataclass
class Resultado:
    tarefa: Tarefa
    status: str            # Convertido | Sem ganho | Ignorado | Erro | Cancelado
    mensagem: str = ""
    bytes_origem: int = 0
    bytes_destino: int = 0
    vmaf: float | None = None

    @property
    def reducao(self) -> float | None:
        if self.status not in ("Convertido", "Sem ganho") or not self.bytes_origem:
            return None
        return 100.0 * (1 - self.bytes_destino / self.bytes_origem)


def verificar_ferramentas() -> str | None:
    """Devolve uma mensagem de erro se ffmpeg/ffprobe/libsvtav1 não estiverem disponíveis."""
    if not _achar("ffmpeg") or not _achar("ffprobe"):
        return ("ffmpeg/ffprobe não foram encontrados no PATH.\n\n"
                "Instale o ffmpeg (por exemplo: winget install Gyan.FFmpeg), "
                "feche e abra o terminal e tente de novo.")
    try:
        saida = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True,
                               text=True, errors="replace", creationflags=SEM_JANELA).stdout
    except OSError as e:
        return f"Não foi possível executar o ffmpeg: {e}"
    if "libsvtav1" not in saida:
        return ("O ffmpeg instalado não tem o codificador libsvtav1 (SVT-AV1).\n"
                "Instale uma versão 'full' do ffmpeg.")
    return None


def planejar(raiz: Path, subpastas: bool, pasta_saida: Path | None) -> list[Tarefa]:
    """Lista os vídeos da pasta e define o nome do arquivo reduzido de cada um.

    pasta_saida=None  -> reduzido ao lado do original, com sufixo ' - RED'.
    pasta_saida=Path  -> reduzido em pasta separada, espelhando as subpastas.
    """
    raiz = raiz.resolve()
    saida = pasta_saida.resolve() if pasta_saida else None
    arquivos = raiz.rglob("*") if subpastas else raiz.glob("*")
    tarefas: list[Tarefa] = []
    usados: set[Path] = set()
    for arq in sorted(arquivos):
        if not arq.is_file() or arq.suffix.lower() not in EXTENSOES:
            continue
        if arq.stem.endswith(SUFIXOS_DE_SAIDA) or MARCA_PARCIAL in arq.suffixes:
            continue                      # já é uma saída nossa
        if saida and saida in arq.parents:
            continue                      # não reconverter a pasta de saída
        base = (saida / arq.relative_to(raiz).parent) if saida else arq.parent
        tarefas.append(Tarefa(arq, _destino(arq, base, not saida, usados)))
    return tarefas


def _destino(arq: Path, base: Path, com_sufixo: bool, usados: set[Path]) -> Path:
    """Nome do arquivo reduzido de `arq` dentro de `base`, sem colidir com outro já planejado."""
    ext = ".mp4" if arq.suffix.lower() in FAMILIA_MP4 else ".mkv"
    sufixo = SUFIXO if com_sufixo else ""
    destino = base / (arq.stem + sufixo + ext)
    if destino in usados or destino == arq:   # ex.: a.mp4 e a.mov na mesma pasta
        destino = base / f"{arq.stem}_{arq.suffix[1:].lower()}{sufixo}{ext}"
    usados.add(destino)
    return destino


def planejar_arquivos(arquivos: list[Path], pasta_saida: Path | None) -> list[Tarefa]:
    """Como planejar(), mas para uma lista de arquivos soltos (resultado da busca de vídeos grandes).

    pasta_saida=None  -> reduzido ao lado do original, com sufixo ' - RED'.
    pasta_saida=Path  -> reduzido na pasta separada, repetindo o caminho do original
                         (ex.: D:\\Aulas\\a.mp4 -> <saída>\\D\\Aulas\\a.mp4).
    """
    saida = pasta_saida.resolve() if pasta_saida else None
    tarefas: list[Tarefa] = []
    usados: set[Path] = set()
    for arq in arquivos:
        arq = arq.resolve()
        if saida and saida in arq.parents:
            continue
        if saida:
            unidade = arq.drive.rstrip(":").strip("\\/").replace("\\", "_").replace("/", "_")
            base = saida / unidade / arq.relative_to(arq.anchor).parent
        else:
            base = arq.parent
        tarefas.append(Tarefa(arq, _destino(arq, base, not saida, usados)))
    return tarefas


# --------------------------------------------------------------------------
# Busca de vídeos grandes no computador
# --------------------------------------------------------------------------
TAMANHO_MINIMO_MB = 500
PASTAS_IGNORADAS = {"$recycle.bin", "system volume information", "appdata"}          # em qualquer nível
PASTAS_IGNORADAS_NA_RAIZ = {"windows", "program files", "program files (x86)", "programdata",
                            "$windows.~bt", "$windows.~ws", "recovery"}               # só na raiz do disco


def unidades_locais() -> list[Path]:
    """Discos fixos do computador (C:\\, D:\\ ...). Fora do Windows, a pasta do usuário."""
    if os.name != "nt":
        return [Path.home()]
    try:
        import ctypes
        import string
        k32 = ctypes.windll.kernel32
        mascara = k32.GetLogicalDrives()
        discos = [Path(f"{letra}:\\") for i, letra in enumerate(string.ascii_uppercase)
                  if (mascara >> i) & 1 and k32.GetDriveTypeW(f"{letra}:\\") == 3]   # 3 = disco fixo
    except Exception:
        discos = []
    return discos or [Path.home()]


def ja_tem_reduzido(arq: Path) -> bool:
    """True se já existe, ao lado do original, um reduzido gerado por este aplicativo."""
    ext = ".mp4" if arq.suffix.lower() in FAMILIA_MP4 else ".mkv"
    return any((arq.parent / (arq.stem + s + ext)).exists() for s in SUFIXOS_DE_SAIDA)


def procurar_grandes(raizes: list[Path], minimo_bytes: int, cancelar=None,
                     ao_achar=None, ao_andar=None) -> list[tuple[Path, int]]:
    """Varre as pastas (com subpastas) e devolve [(vídeo, bytes)] com tamanho >= minimo_bytes,
    do maior para o menor. Pastas de sistema, atalhos de pasta e saídas deste aplicativo ficam de fora;
    pastas sem permissão de leitura são puladas em silêncio."""
    achados: list[tuple[Path, int]] = []
    pilha = [Path(r) for r in reversed(list(raizes))]
    while pilha:
        if cancelar is not None and cancelar.is_set():
            break
        pasta = pilha.pop()
        if ao_andar:
            ao_andar(pasta)
        na_raiz = pasta.parent == pasta
        try:
            with os.scandir(pasta) as it:
                itens = list(it)
        except OSError:
            continue
        subpastas = []
        for e in itens:
            try:
                if e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction()):
                    continue
                if e.is_dir(follow_symlinks=False):
                    nome = e.name.lower()
                    if nome in PASTAS_IGNORADAS or (na_raiz and nome in PASTAS_IGNORADAS_NA_RAIZ):
                        continue
                    subpastas.append(Path(e.path))
                elif e.is_file(follow_symlinks=False):
                    arq = Path(e.path)
                    if arq.suffix.lower() not in EXTENSOES:
                        continue
                    if arq.stem.endswith(SUFIXOS_DE_SAIDA) or MARCA_PARCIAL in arq.suffixes:
                        continue                      # já é uma saída nossa
                    tam = e.stat(follow_symlinks=False).st_size
                    if tam >= minimo_bytes:
                        achados.append((arq, tam))
                        if ao_achar:
                            ao_achar(arq, tam)
            except OSError:
                continue
        pilha.extend(sorted(subpastas, reverse=True))
    return sorted(achados, key=lambda par: (-par[1], str(par[0]).lower()))


def sondar(arquivo: Path) -> dict:
    """Codec, pix_fmt, ordem dos campos e duração (s) do primeiro fluxo de vídeo. {} se não houver vídeo."""
    cmd = [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
           "stream=codec_name,pix_fmt,field_order:format=duration", "-of", "json", str(arquivo)]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                       creationflags=SEM_JANELA)
    try:
        dados = json.loads(r.stdout or "{}")
        fluxo = dados["streams"][0]
    except (ValueError, KeyError, IndexError):
        return {}
    try:
        duracao = float(dados.get("format", {}).get("duration", 0))
    except (TypeError, ValueError):
        duracao = 0.0
    return {"codec": fluxo.get("codec_name", ""), "pix_fmt": fluxo.get("pix_fmt", ""),
            "campos": fluxo.get("field_order", ""), "duracao": duracao}


FILTRO_DESENTRELACAR = "bwdif=mode=send_frame:parity=auto:deint=all"   # mantém a taxa de quadros


def entrelacado(arquivo: Path, info: dict) -> bool:
    """True se o vídeo é entrelaçado (comum em .mpg, DVD, TV e filmadoras antigas).

    Arquivo marcado como progressivo é aceito como tal. Nos demais, a marcação do arquivo não é
    confiável, então o filtro idet do ffmpeg examina uma amostra de 300 quadros."""
    campos = info.get("campos", "")
    if campos == "progressive":
        return False
    inicio = info.get("duracao", 0) * 0.1 if info.get("duracao", 0) > 30 else 0
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "info", "-nostats", "-ss", f"{inicio:.2f}",
           "-i", str(arquivo), "-map", "0:v:0", "-frames:v", "300", "-vf", "idet", "-an", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=180,
                           creationflags=SEM_JANELA)
        achados = re.findall(r"Multi frame detection: TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)",
                             r.stderr)
    except (OSError, subprocess.SubprocessError):
        achados = []
    if achados:
        tff, bff, prog = (int(n) for n in achados[-1])
        maior, menor = max(tff, bff), min(tff, bff)
        if maior + menor + prog:
            # Entrelaçado de verdade tem uma ordem de campos só e ela predomina; quando as duas
            # ordens aparecem misturadas, é imagem progressiva com detalhes finos enganando o detector.
            return maior > prog and menor * 10 <= maior
    return campos in ("tt", "bb", "tb", "bt")


def duracao_a_converter(tarefa: Tarefa) -> float:
    """Segundos de vídeo que esta tarefa vai de fato codificar (0 se for ignorada)."""
    if tarefa.destino.exists():
        return 0.0
    info = sondar(tarefa.origem)
    if not info or info["codec"] == "av1":
        return 0.0
    return info["duracao"]


def _rodar_ffmpeg(cmd, duracao, ao_progredir, cancelar, ocultar: Path | None = None) -> tuple[int, str]:
    with tempfile.TemporaryFile() as erros:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=erros,
                                stdin=subprocess.DEVNULL, text=True, errors="replace",
                                creationflags=SEM_JANELA)
        for linha in proc.stdout:
            if cancelar.is_set():
                proc.terminate()
                break
            if ocultar is not None and ocultar.exists():
                _ocultar(ocultar)
                ocultar = None
            if linha.startswith(("out_time_us=", "out_time_ms=")) and duracao > 0:
                try:
                    seg = int(linha.split("=", 1)[1]) / 1_000_000   # ambos em microssegundos
                    ao_progredir(max(0.0, min(1.0, seg / duracao)))
                except ValueError:
                    pass
        codigo = proc.wait()
        erros.seek(0)
        texto = erros.read().decode("utf-8", "replace")
    return codigo, texto


def medir_vmaf(original: Path, reduzido: Path, duracao: float, ao_progredir, cancelar,
               desentrelacar: bool = False) -> float | None:
    """Índice VMAF (0 a 100) do arquivo reduzido em relação ao original.
    Se o reduzido foi desentrelaçado, o original passa pelo mesmo filtro antes da comparação."""
    exe = ferramenta_vmaf()
    if not exe:
        return None
    grafo = ("[0:v]setpts=PTS-STARTPTS,format=yuv420p[d];[1:v]"
             + (FILTRO_DESENTRELACAR + "," if desentrelacar else "") + "setpts=PTS-STARTPTS,format=yuv420p[r];"
             f"[d][r]libvmaf=n_threads={os.cpu_count() or 2}")
    cmd = [exe, "-hide_banner", "-nostdin", "-loglevel", "info", "-nostats", "-progress", "pipe:1",
           "-i", str(reduzido), "-i", str(original), "-lavfi", grafo, "-an", "-f", "null", "-"]
    codigo, texto = _rodar_ffmpeg(cmd, duracao, ao_progredir, cancelar)
    achado = re.search(r"VMAF score:\s*([0-9.]+)", texto)
    return float(achado.group(1)) if codigo == 0 and achado else None


def converter(tarefa: Tarefa, crf: int, preset: int, ao_progredir, cancelar,
              vmaf: bool = False, ao_etapa=None, apagar_original: bool = False) -> Resultado:
    origem, destino = tarefa.origem, tarefa.destino
    tam_origem = origem.stat().st_size
    if destino.exists():
        return Resultado(tarefa, "Ignorado", "arquivo reduzido já existe", tam_origem,
                         destino.stat().st_size)
    info = sondar(origem)
    if not info:
        return Resultado(tarefa, "Ignorado", "sem fluxo de vídeo legível", tam_origem)
    if info["codec"] == "av1":
        return Resultado(tarefa, "Ignorado", "original já está em AV1", tam_origem)

    destino.parent.mkdir(parents=True, exist_ok=True)
    parcial = destino.with_name(destino.stem + MARCA_PARCIAL + destino.suffix)
    pix = "yuv420p10le" if any(b in info["pix_fmt"] for b in ("10", "12")) else "yuv420p"
    desentrelacar = entrelacado(origem, info)

    def comando(audio: list[str]) -> list[str]:
        cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-nostats",
               "-progress", "pipe:1", "-i", str(origem),
               "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0",
               *(["-vf", FILTRO_DESENTRELACAR] if desentrelacar else []),
               "-c:v", "libsvtav1", "-preset", str(preset), "-crf", str(crf), "-pix_fmt", pix,
               *audio]
        if destino.suffix == ".mp4":
            cmd += ["-movflags", "+faststart"]
        return cmd + [str(parcial)]

    def limpar():
        try:
            parcial.unlink()
        except OSError:
            pass

    # 1ª tentativa copia o áudio sem recodificar; se o container recusar, recodifica em AAC.
    peso = 0.8 if vmaf and ferramenta_vmaf() else 1.0     # fatia do progresso que cabe à conversão
    limpar()                                              # sobra oculta de execução interrompida
    codigo, erros = _rodar_ffmpeg(comando(["-c:a", "copy"]), info["duracao"],
                                  lambda p: ao_progredir(p * peso), cancelar, parcial)
    if codigo != 0 and not cancelar.is_set():
        limpar()
        codigo, erros = _rodar_ffmpeg(comando(["-c:a", "aac", "-b:a", "128k"]), info["duracao"],
                                      lambda p: ao_progredir(p * peso), cancelar, parcial)
    if cancelar.is_set():
        limpar()
        return Resultado(tarefa, "Cancelado", "", tam_origem)
    if codigo != 0:
        limpar()
        linhas = [l for l in erros.strip().splitlines() if not l.startswith("Svt[")]
        return Resultado(tarefa, "Erro", (linhas[-1] if linhas else f"ffmpeg saiu com código {codigo}"),
                         tam_origem)

    # Só promove o arquivo depois de conferir que ele abre e tem a duração esperada.
    conf = sondar(parcial)
    tolerancia = max(2.0, info["duracao"] * 0.02)
    if conf.get("codec") != "av1" or (info["duracao"] and abs(conf["duracao"] - info["duracao"]) > tolerancia):
        limpar()
        return Resultado(tarefa, "Erro", "arquivo convertido não passou na conferência", tam_origem)
    nota = None
    if peso < 1.0:
        if ao_etapa:
            ao_etapa("Medindo VMAF...")
        nota = medir_vmaf(origem, parcial, info["duracao"],
                          lambda p: ao_progredir(peso + (1 - peso) * p), cancelar, desentrelacar)
        if cancelar.is_set():
            limpar()
            return Resultado(tarefa, "Cancelado", "", tam_origem)
    _ocultar(parcial, False)
    try:
        st = origem.stat()
        os.utime(parcial, (st.st_atime, st.st_mtime))   # preserva a data do original
    except OSError:
        pass
    parcial.replace(destino)
    tam_destino = destino.stat().st_size
    status = "Convertido" if tam_destino < tam_origem else "Sem ganho"
    notas = [] if status == "Convertido" else ["reduzido ficou maior ou igual ao original"]
    if desentrelacar:
        notas.append("desentrelaçado")
    if apagar_original and status == "Convertido":
        # Só chega aqui com o reduzido conferido, gravado no destino e menor que o original.
        try:
            origem.unlink()
            notas.append("original apagado")
        except OSError as e:
            notas.append(f"não foi possível apagar o original ({e.strerror or e})")
    return Resultado(tarefa, status, "; ".join(notas), tam_origem, tam_destino, nota)


def registrar(raiz: Path, r: Resultado) -> None:
    """Acrescenta uma linha ao CSV de controle (abre direto no Excel pt-BR)."""
    if r.status == "Ignorado" and r.bytes_destino:
        return   # já convertido em rodada anterior: não repetir no log
    caminho = raiz / NOME_LOG
    novo = not caminho.exists()
    try:
        with open(caminho, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            if novo:
                w.writerow(["data_hora", "original", "reduzido", "bytes_original", "bytes_reduzido",
                            "reducao_pct", "status", "mensagem", "vmaf"])
            red = "" if r.reducao is None else f"{r.reducao:.1f}".replace(".", ",")
            nota = "" if r.vmaf is None else f"{r.vmaf:.2f}".replace(".", ",")
            w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), str(r.tarefa.origem),
                        str(r.tarefa.destino), r.bytes_origem, r.bytes_destino or "", red,
                        r.status, r.mensagem, nota])
        lembrar_log(caminho)
    except OSError:
        pass   # log é auxiliar; nunca interrompe a conversão


def tamanho(n: int) -> str:
    v = float(n)
    for un in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or un == "TB":
            return f"{v:.0f} {un}" if un == "B" else f"{v:.1f} {un}"
        v /= 1024
    return ""


def tempo(segundos: float) -> str:
    s = int(round(max(0, segundos)))
    h, resto = divmod(s, 3600)
    m, s = divmod(resto, 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {s:02d} s"
    return f"{s} s"


def resumo(resultados: list[Resultado]) -> str:
    feitos = [r for r in resultados if r.status in ("Convertido", "Sem ganho")]
    antes = sum(r.bytes_origem for r in feitos)
    depois = sum(r.bytes_destino for r in feitos)
    erros = sum(r.status == "Erro" for r in resultados)
    ignorados = sum(r.status == "Ignorado" for r in resultados)
    txt = f"{len(feitos)} convertido(s), {ignorados} ignorado(s), {erros} erro(s)."
    if antes:
        txt += f"  {tamanho(antes)} -> {tamanho(depois)} ({100 * (1 - depois / antes):.0f}% menor)."
    notas = [r.vmaf for r in feitos if r.vmaf is not None]
    if notas:
        txt += f"  VMAF médio {sum(notas) / len(notas):.1f}."
    return txt


# --------------------------------------------------------------------------
# Modo terminal
# --------------------------------------------------------------------------
def rodar_cli(args) -> int:
    erro = verificar_ferramentas()
    if erro:
        print(erro, file=sys.stderr)
        return 2
    raiz = Path(args.cli)
    if not raiz.is_dir():
        print(f"Pasta não encontrada: {raiz}", file=sys.stderr)
        return 2
    tarefas = planejar(raiz, not args.sem_subpastas, Path(args.saida) if args.saida else None)
    print(f"{len(tarefas)} vídeo(s) encontrado(s) em {raiz}")
    cancelar = threading.Event()
    resultados = []
    inicio = time.monotonic()
    try:
        for i, t in enumerate(tarefas, 1):
            rotulo = f"[{i}/{len(tarefas)}] {t.origem.name}"
            r = converter(t, args.crf, args.preset,
                          lambda p: print(f"\r{rotulo}  {p * 100:5.1f}%", end="", flush=True), cancelar,
                          vmaf=not args.sem_vmaf, apagar_original=args.apagar_originais)
            registrar(raiz, r)
            resultados.append(r)
            extra = f" ({r.reducao:.0f}% menor)" if r.reducao is not None else ""
            extra += f" VMAF {r.vmaf:.1f}" if r.vmaf is not None else ""
            print(f"\r{rotulo}  {r.status}{extra} {r.mensagem}".ljust(79))
    except KeyboardInterrupt:
        cancelar.set()
        print("\nCancelado pelo usuário.")
    print(resumo(resultados), f" Tempo: {tempo(time.monotonic() - inicio)}.")
    return 1 if any(r.status == "Erro" for r in resultados) else 0


# Colunas da consulta: as mesmas da tela de conversão, com a data e hora na frente.
ID_APP = "compactador"
CHAVE_DATA, CHAVE_ARQUIVO, CHAVE_SAIDA, CHAVE_MSG = "data_hora", "original", "reduzido", "mensagem"
COLUNAS_REG = (("data", "Data/hora", 135, "w"), ("arquivo", "Arquivo", 300, "w"),
               ("status", "Situação", 230, "w"), ("original", "Original", 90, "e"),
               ("reduzido", "Reduzido", 90, "e"), ("reducao", "Redução", 95, "e"), ("vmaf", "VMAF", 95, "e"))


def registro_para_tela(r: dict) -> tuple[tuple, tuple]:
    """(valores exibidos, chaves de ordenação) de uma linha do CSV."""
    nome = Path(r.get("original", "")).name
    st, msg = r.get("status", ""), r.get("mensagem", "")
    orig, red = _reg_num(r.get("bytes_original", "")), _reg_num(r.get("bytes_reduzido", ""))
    pct, nota = _reg_num(r.get("reducao_pct", "")), _reg_num(r.get("vmaf", ""))
    valores = (_reg_data_txt(r), nome, st + (f" - {msg}" if msg else ""),
               tamanho(int(orig)) if orig else "", tamanho(int(red)) if red else "",
               f"{pct:.0f}%" if pct is not None else "", f"{nota:.1f}" if nota is not None else "")
    chaves = (r["_data"] or datetime.min, nome.lower(), st.lower(), orig or 0, red or 0, _reg_k(pct), _reg_k(nota))
    return valores, chaves


def total_registros(regs: list) -> tuple:
    """Última linha, como na tela de conversão: totais e médias."""
    feitos = [r for r in regs if r.get("status") in ("Convertido", "Sem ganho")]
    original = sum(_reg_num(r.get("bytes_original", "")) or 0 for r in regs)
    reduzido = sum(_reg_num(r.get("bytes_reduzido", "")) or 0 for r in feitos)
    media, nota = _reg_media(feitos, "reducao_pct"), _reg_media(feitos, "vmaf")
    return ("", f"TOTAL  ({len(regs)} registro(s))", f"{len(feitos)} convertido(s) de {len(regs)}",
            tamanho(int(original)), tamanho(int(reduzido)) if feitos else "",
            f"média {media:.0f}%" if media is not None else "", f"média {nota:.1f}" if nota is not None else "")


# --------------------------------------------------------------------------
# Consulta dos registros (CSV de controle gravado em cada pasta processada)
# --------------------------------------------------------------------------
PASTA_CONFIG = Path(os.environ.get("APPDATA") or (Path.home() / ".config")) / "CC" / ID_APP
INDICE_LOGS = PASTA_CONFIG / "registros_conhecidos.txt"
SITUACOES = ("Convertido", "Sem ganho", "Ignorado", "Erro")
_LEMBRADOS: set = set()
_TRAVA_INDICE = threading.Lock()


def logs_conhecidos(so_existentes: bool = True) -> list[Path]:
    """CSVs de controle já gravados por este programa (o índice fica no perfil do usuário)."""
    try:
        linhas = INDICE_LOGS.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    vistos, logs = set(), []
    for linha in linhas:
        linha = linha.strip()
        chave = os.path.normcase(linha)
        if not linha or chave in vistos:
            continue
        vistos.add(chave)
        if not so_existentes or Path(linha).is_file():
            logs.append(Path(linha))
    return logs


def lembrar_log(caminho: Path) -> None:
    """Anota o CSV no índice, para a consulta encontrá-lo depois, qualquer que seja a pasta."""
    try:
        texto = str(Path(caminho).resolve())
        chave = os.path.normcase(texto)
        with _TRAVA_INDICE:
            if not _LEMBRADOS:
                _LEMBRADOS.update(os.path.normcase(str(p)) for p in logs_conhecidos(False))
            if chave in _LEMBRADOS:
                return
            PASTA_CONFIG.mkdir(parents=True, exist_ok=True)
            with open(INDICE_LOGS, "a", encoding="utf-8") as f:
                f.write(texto + "\n")
            _LEMBRADOS.add(chave)
    except OSError:
        pass   # o índice é auxiliar; nunca interrompe a conversão


def procurar_logs(pasta: Path) -> list[Path]:
    """Procura CSVs de controle numa pasta e subpastas (registros anteriores ao índice) e os anota."""
    achados = []
    for raiz, dirs, arqs in os.walk(pasta, onerror=lambda e: None):
        dirs[:] = [d for d in dirs if not d.startswith((".", "$")) and d.lower() not in ("appdata", "windows")]
        if NOME_LOG in arqs:
            achados.append(Path(raiz) / NOME_LOG)
            lembrar_log(achados[-1])
    return achados


def _reg_num(texto) -> float | None:
    try:
        return float(str(texto).strip().replace(",", "."))
    except ValueError:
        return None


def _reg_data(texto: str):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(texto.strip(), fmt)
        except ValueError:
            pass
    return None


def _reg_data_txt(r: dict) -> str:
    return r["_data"].strftime("%d/%m/%Y %H:%M:%S") if r["_data"] else r.get(CHAVE_DATA, "")


def _reg_k(valor) -> float:
    return -1.0 if valor is None else valor


def _reg_media(regs: list, chave: str) -> float | None:
    notas = [n for n in (_reg_num(r.get(chave, "")) for r in regs) if n is not None]
    return sum(notas) / len(notas) if notas else None


def ler_log(caminho: Path) -> list[dict]:
    """Linhas do CSV como dicionários (chaves = cabeçalho), mais a data lida e o caminho do log."""
    try:
        with open(caminho, newline="", encoding="utf-8-sig", errors="replace") as f:
            linhas = list(csv.reader(f, delimiter=";"))
    except OSError:
        return []
    if not linhas:
        return []
    cabecalho, regs = linhas[0], []
    for linha in linhas[1:]:
        if not any(c.strip() for c in linha):
            continue
        r = dict(zip(cabecalho, linha))
        r["_data"] = _reg_data(r.get(CHAVE_DATA, ""))
        r["_log"] = str(caminho)
        regs.append(r)
    return regs


def filtrar_registros(regs: list, texto: str = "", situacao: str = "", de=None, ate=None) -> list:
    texto, situacao = texto.strip().lower(), situacao.strip().lower()
    saida = []
    for r in regs:
        if situacao and r.get("status", "").lower() != situacao:
            continue
        if texto and texto not in (r.get(CHAVE_ARQUIVO, "") + " " + r.get(CHAVE_MSG, "")).lower():
            continue
        if de and (r["_data"] is None or r["_data"].date() < de):
            continue
        if ate and (r["_data"] is None or r["_data"].date() > ate):
            continue
        saida.append(r)
    return saida


def abrir_externo(caminho: Path, selecionar: bool = False) -> None:
    """Abre o arquivo no programa padrão, ou mostra-o selecionado no Explorador."""
    try:
        if os.name == "nt":
            if selecionar:
                subprocess.Popen(["explorer", "/select,", str(caminho)])
            else:
                os.startfile(str(caminho))
        else:
            subprocess.Popen(["xdg-open", str(caminho.parent if selecionar else caminho)])
    except OSError:
        pass


def imprimir_registros(alvo: str = "", situacao: str = "") -> int:
    """Modo terminal: mostra os registros no mesmo formato da tabela da janela."""
    if alvo:
        p = Path(alvo).expanduser()
        logs = [p] if p.is_file() else (procurar_logs(p) if p.is_dir() else [])
    else:
        logs = logs_conhecidos()
    regs = filtrar_registros([r for log in logs for r in ler_log(log)], situacao=situacao or "")
    if not regs:
        print("Nenhum registro encontrado.")
        return 1
    regs.sort(key=lambda r: r["_data"] or datetime.min)
    linhas = [tuple(c[1] for c in COLUNAS_REG)] + [registro_para_tela(r)[0] for r in regs] + \
             [total_registros(regs)]
    linhas = [tuple(str(v)[:60] for v in lin) for lin in linhas]
    larg = [max(len(lin[i]) for lin in linhas) for i in range(len(COLUNAS_REG))]
    for k, lin in enumerate(linhas):
        if k == len(linhas) - 1:
            print("-" * (sum(larg) + 2 * len(larg)))
        print("  ".join(v.ljust(w) if c[3] == "w" else v.rjust(w) for v, w, c in zip(lin, larg, COLUNAS_REG)))
        if k == 0:
            print("-" * (sum(larg) + 2 * len(larg)))
    print(f"{len(regs)} registro(s), de {len(logs)} arquivo(s) de log.")
    return 0


def abrir_registros(janela, sugestoes=()) -> None:
    """Janela de consulta: mesmas colunas da tela de conversão, mais a data e hora do processamento."""
    import tkinter as tk
    from tkinter import filedialog, ttk
    from tkinter import font as tkfont

    for pasta in sugestoes:
        try:
            if pasta and (Path(pasta) / NOME_LOG).is_file():
                lembrar_log(Path(pasta) / NOME_LOG)
        except OSError:
            pass
    fonte = tkfont.nametofont("TkDefaultFont")
    negrito = (fonte.actual("family"), fonte.actual("size"), "bold")

    dlg = tk.Toplevel(janela)
    dlg.title(f"C&C - {TITULO} - Registros")
    dlg.geometry("1220x640")
    dlg.minsize(920, 440)
    dlg.configure(bg=FUNDO)
    dlg.transient(janela)
    TODOS = "Todos os registros conhecidos"
    v_fonte = tk.StringVar(value=TODOS)
    v_texto = tk.StringVar()
    v_sit = tk.StringVar(value="Todas")
    v_de = tk.StringVar()
    v_ate = tk.StringVar()
    v_info = tk.StringVar()
    dados = {"logs": [], "regs": [], "vis": [], "n_logs": 0, "ordem": (0, True)}

    cima = ttk.Frame(dlg, padding=(14, 12, 14, 6))
    cima.pack(fill="x")
    cima.columnconfigure(1, weight=1)
    ttk.Label(cima, text="Registro:", style="Rotulo.TLabel").grid(row=0, column=0, sticky="w")
    c_fonte = ttk.Combobox(cima, textvariable=v_fonte, state="readonly")
    c_fonte.grid(row=0, column=1, sticky="ew", padx=8)
    ttk.Button(cima, text="Abrir arquivo...", command=lambda: abrir_arquivo()).grid(row=0, column=2)
    ttk.Button(cima, text="Procurar em pasta...", command=lambda: procurar_pasta()).grid(
        row=0, column=3, padx=(6, 0))
    filtros = ttk.Frame(cima)
    filtros.grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))
    ttk.Label(filtros, text="Filtrar:", style="Rotulo.TLabel").pack(side="left")
    ttk.Label(filtros, text="Arquivo contém").pack(side="left", padx=(10, 0))
    ttk.Entry(filtros, textvariable=v_texto, width=26).pack(side="left", padx=(4, 0))
    ttk.Label(filtros, text="Situação").pack(side="left", padx=(14, 0))
    c_sit = ttk.Combobox(filtros, textvariable=v_sit, state="readonly", width=12,
                         values=("Todas",) + SITUACOES)
    c_sit.pack(side="left", padx=(4, 0))
    ttk.Label(filtros, text="Processado de").pack(side="left", padx=(14, 0))
    e_de = ttk.Entry(filtros, textvariable=v_de, width=11)
    e_de.pack(side="left", padx=(4, 0))
    ttk.Label(filtros, text="até").pack(side="left", padx=(6, 0))
    e_ate = ttk.Entry(filtros, textvariable=v_ate, width=11)
    e_ate.pack(side="left", padx=(4, 0))
    ttk.Label(filtros, text="dd/mm/aaaa", style="Dica.TLabel").pack(side="left", padx=(6, 0))
    ttk.Button(filtros, text="Limpar", command=lambda: limpar()).pack(side="left", padx=(14, 0))

    # Rodapé antes da tabela, para a tabela ocupar o espaço que sobrar.
    base = ttk.Frame(dlg, padding=(14, 8, 14, 12))
    base.pack(side="bottom", fill="x")
    base.columnconfigure(0, weight=1)
    ttk.Label(base, textvariable=v_info, wraplength=720, justify="left").grid(row=0, column=0, sticky="w")
    ttk.Button(base, text="Atualizar", command=lambda: carregar()).grid(row=0, column=1, padx=(6, 0))
    ttk.Button(base, text="Exportar...", command=lambda: exportar()).grid(row=0, column=2, padx=(6, 0))
    ttk.Button(base, text="Abrir planilha", command=lambda: abrir_planilha()).grid(row=0, column=3, padx=(6, 0))
    ttk.Button(base, text="Fechar", command=dlg.destroy).grid(row=0, column=4, padx=(6, 0))
    ttk.Label(base, text="Clique no título de uma coluna para ordenar; duplo clique numa linha mostra o "
                         "vídeo na pasta.", style="Dica.TLabel").grid(row=1, column=0, columnspan=5,
                                                                      sticky="w", pady=(4, 0))

    meio = ttk.Frame(dlg, padding=(14, 0))
    meio.pack(fill="both", expand=True)
    ids = [f"c{i}" for i in range(len(COLUNAS_REG))]
    tabela = ttk.Treeview(meio, columns=ids, show="headings", selectmode="browse")
    for i, (cid, (nome, titulo, larg, anc)) in enumerate(zip(ids, COLUNAS_REG)):
        tabela.heading(cid, text=titulo, anchor=anc, command=lambda i=i: ordenar(i))
        tabela.column(cid, width=larg, minwidth=40, anchor=anc, stretch=(nome == "arquivo"))
    tabela.tag_configure("total", background=AZUL_CLARO, foreground=AZUL_ESCURO, font=negrito)
    tabela.tag_configure("erro", foreground="#B42318")
    rol_v = ttk.Scrollbar(meio, orient="vertical", command=tabela.yview)
    rol_h = ttk.Scrollbar(meio, orient="horizontal", command=tabela.xview)
    tabela.configure(yscrollcommand=rol_v.set, xscrollcommand=rol_h.set)
    rol_h.pack(side="bottom", fill="x")
    tabela.pack(side="left", fill="both", expand=True)
    rol_v.pack(side="right", fill="y")

    def data_do_campo(var):
        texto = var.get().strip()
        if not texto:
            return None
        for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
            try:
                return datetime.strptime(texto, fmt).date()
            except ValueError:
                pass
        raise ValueError(texto)

    def atualizar_fontes():
        dados["logs"] = logs_conhecidos()
        c_fonte["values"] = [TODOS] + [str(p) for p in dados["logs"]]
        if v_fonte.get() not in c_fonte["values"]:
            v_fonte.set(TODOS)

    def carregar():
        atualizar_fontes()
        logs = dados["logs"] if v_fonte.get() == TODOS else [Path(v_fonte.get())]
        dados["regs"] = [r for log in logs for r in ler_log(log)]
        dados["n_logs"] = len(logs)
        filtrar()

    def filtrar(*_):
        try:
            de, ate = data_do_campo(v_de), data_do_campo(v_ate)
        except ValueError as e:
            v_info.set(f"Data inválida: {e}. Use dd/mm/aaaa.")
            return
        sit = "" if v_sit.get() == "Todas" else v_sit.get()
        telas = [(r,) + registro_para_tela(r)
                 for r in filtrar_registros(dados["regs"], v_texto.get(), sit, de, ate)]
        col, desc = dados["ordem"]
        telas.sort(key=lambda t: t[2][col], reverse=desc)
        dados["vis"] = [t[0] for t in telas]
        tabela.delete(*tabela.get_children())
        for i, (r, valores, _) in enumerate(telas):
            tabela.insert("", "end", iid=f"r{i}", values=valores,
                          tags=("erro",) if r.get("status") == "Erro" else ())
        if telas:
            tabela.insert("", "end", iid="total", values=total_registros(dados["vis"]), tags=("total",))
        for i, cid in enumerate(ids):
            seta = (" ▼" if desc else " ▲") if i == col else ""
            tabela.heading(cid, text=COLUNAS_REG[i][1] + seta)
        if not dados["regs"]:
            v_info.set("Nenhum registro encontrado. Os vídeos aparecem aqui depois de processados; para "
                       "registros gravados antes desta versão, use \"Procurar em pasta...\".")
        else:
            v_info.set(f"{len(telas)} de {len(dados['regs'])} registro(s), "
                       f"de {dados['n_logs']} arquivo(s) de log.")

    def ordenar(i):
        col, desc = dados["ordem"]
        dados["ordem"] = (i, (not desc) if col == i else i == 0)
        filtrar()

    def limpar():
        v_texto.set("")
        v_sit.set("Todas")
        v_de.set("")
        v_ate.set("")
        filtrar()

    def abrir_arquivo():
        p = filedialog.askopenfilename(parent=dlg, title="Abrir registro",
                                       filetypes=[("Registro (CSV)", "*.csv"), ("Todos os arquivos", "*.*")])
        if p:
            lembrar_log(Path(p))
            atualizar_fontes()
            v_fonte.set(str(Path(p).resolve()))
            carregar()

    def procurar_pasta():
        d = filedialog.askdirectory(parent=dlg, title="Procurar registros nesta pasta e nas subpastas")
        if not d:
            return
        v_info.set("Procurando registros...")
        dlg.configure(cursor="watch")
        dlg.update_idletasks()
        try:
            achados = procurar_logs(Path(d))
        finally:
            dlg.configure(cursor="")
        v_fonte.set(TODOS)
        carregar()
        v_info.set(f"{len(achados)} arquivo(s) de log encontrado(s) em {d}.  " + v_info.get())

    def gravar_csv(destino: Path) -> None:
        with open(destino, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow([c[1] for c in COLUNAS_REG] + ["Caminho do vídeo", "Arquivo de log"])
            for r in dados["vis"]:
                w.writerow(list(registro_para_tela(r)[0]) + [r.get(CHAVE_ARQUIVO, ""), r["_log"]])

    def exportar():
        if not dados["vis"]:
            v_info.set("Nada para exportar com os filtros atuais.")
            return
        p = filedialog.asksaveasfilename(parent=dlg, title="Exportar registros", defaultextension=".csv",
                                         initialfile="registros_filtrados.csv",
                                         filetypes=[("Planilha CSV", "*.csv")])
        if p:
            try:
                gravar_csv(Path(p))
                v_info.set(f"{len(dados['vis'])} registro(s) exportado(s) para {p}.")
            except OSError as e:
                v_info.set(f"Não foi possível gravar {p}: {e.strerror or e}")

    def abrir_planilha():
        if v_fonte.get() != TODOS:
            abrir_externo(Path(v_fonte.get()))
            return
        if not dados["vis"]:
            v_info.set("Nada para abrir com os filtros atuais.")
            return
        try:
            temp = Path(tempfile.gettempdir()) / f"registros_{ID_APP}_{datetime.now():%Y%m%d_%H%M%S}.csv"
            gravar_csv(temp)
            abrir_externo(temp)
        except OSError as e:
            v_info.set(f"Não foi possível abrir a planilha: {e.strerror or e}")

    def mostrar_na_pasta(ev):
        iid = tabela.identify_row(ev.y)
        if not iid.startswith("r"):
            return
        r = dados["vis"][int(iid[1:])]
        for chave in (CHAVE_ARQUIVO, CHAVE_SAIDA):
            if chave and r.get(chave) and Path(r[chave]).exists():
                abrir_externo(Path(r[chave]), selecionar=True)
                return
        abrir_externo(Path(r["_log"]), selecionar=True)

    v_texto.trace_add("write", filtrar)
    c_sit.bind("<<ComboboxSelected>>", lambda _: (c_sit.selection_clear(), filtrar()))
    c_fonte.bind("<<ComboboxSelected>>", lambda _: (c_fonte.selection_clear(), carregar()))
    for e in (e_de, e_ate):
        e.bind("<Return>", filtrar)
        e.bind("<FocusOut>", filtrar)
    tabela.bind("<Double-1>", mostrar_na_pasta)
    dlg.bind("<Escape>", lambda _: dlg.destroy())
    carregar()
    dlg.focus_set()


# --------------------------------------------------------------------------
# Janela
# --------------------------------------------------------------------------
def rodar_gui() -> int:
    if os.name == "nt":      # Python embarcado na instalação: garante que o Tk seja encontrado
        base = Path(sys.prefix)
        if (base / "Library" / "bin").is_dir():
            try:
                os.add_dll_directory(str(base / "Library" / "bin"))
            except OSError:
                pass
            os.environ["PATH"] = str(base / "Library" / "bin") + os.pathsep + os.environ.get("PATH", "")
        for var, pasta in (("TCL_LIBRARY", "tcl8.6"), ("TK_LIBRARY", "tk8.6")):
            if (base / "Library" / "lib" / pasta).is_dir():
                os.environ.setdefault(var, str(base / "Library" / "lib" / pasta))
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter import font as tkfont

    janela = tk.Tk()
    janela.title(f"C&C - {TITULO} {VERSAO}")
    janela.geometry("1040x700")
    janela.minsize(860, 560)
    janela.configure(bg=FUNDO)

    # ---- padrão visual C&C ----
    fonte = tkfont.nametofont("TkDefaultFont")
    familia = fonte.actual("family")
    negrito = (familia, fonte.actual("size"), "bold")
    est = ttk.Style(janela)
    est.theme_use("clam")
    est.configure(".", background=FUNDO, foreground=TEXTO, bordercolor="#C5CEDF",
                  lightcolor=FUNDO, darkcolor=FUNDO, focuscolor=AZUL)
    est.configure("TButton", padding=(12, 5), background=BRANCO)
    est.map("TButton", background=[("active", AZUL_CLARO), ("disabled", "#ECEFF5")],
            foreground=[("disabled", "#9AA5BA")])
    est.configure("Primario.TButton", background=AZUL, foreground=BRANCO, font=negrito, bordercolor=AZUL)
    est.map("Primario.TButton", background=[("disabled", "#A9B8D4"), ("active", AZUL_ESCURO)],
            foreground=[("disabled", BRANCO)])
    est.configure("TEntry", fieldbackground=BRANCO)
    est.map("TEntry", fieldbackground=[("disabled", "#ECEFF5")])
    est.configure("TSpinbox", fieldbackground=BRANCO, arrowcolor=AZUL)
    est.map("TCheckbutton", background=[("active", FUNDO)], indicatorcolor=[("selected", AZUL)])
    est.map("TRadiobutton", background=[("active", FUNDO)], indicatorcolor=[("selected", AZUL)])
    est.configure("Horizontal.TProgressbar", background=AZUL, troughcolor="#E1E7F2",
                  bordercolor="#E1E7F2", lightcolor=AZUL, darkcolor=AZUL)
    est.configure("Treeview", background=BRANCO, fieldbackground=BRANCO, rowheight=25)
    est.map("Treeview", background=[("selected", AZUL_CLARO)], foreground=[("selected", TEXTO)])
    est.configure("Treeview.Heading", background=AZUL, foreground=BRANCO, font=negrito,
                  relief="flat", padding=(6, 5))
    est.map("Treeview.Heading", background=[("active", AZUL_ESCURO)])
    est.configure("Rotulo.TLabel", font=negrito, foreground=AZUL)
    est.configure("Dica.TLabel", foreground=CINZA)

    # ---- cabeçalho com o logo ----
    cab = tk.Frame(janela, bg=BRANCO)
    cab.pack(fill="x")
    imagens = {}
    try:
        imagens["logo"] = tk.PhotoImage(data=LOGO_B64)
        imagens["icone"] = tk.PhotoImage(data=ICONE_B64)
        janela.iconphoto(True, imagens["icone"])
        tk.Label(cab, image=imagens["logo"], bg=BRANCO).pack(side="left", padx=(14, 12), pady=8)
    except tk.TclError:
        tk.Label(cab, text="C&C", bg=BRANCO, fg=AZUL, font=("Georgia", 24, "bold")).pack(side="left", padx=14, pady=8)
    titulos = tk.Frame(cab, bg=BRANCO)
    titulos.pack(side="left", pady=8)
    tk.Label(titulos, text=TITULO, bg=BRANCO, fg=AZUL, font=(familia, 18, "bold")).pack(anchor="w")
    tk.Label(titulos, text="Redução do tamanho de vídeos em lote, mantendo os originais",
             bg=BRANCO, fg=CINZA).pack(anchor="w")
    tk.Frame(janela, bg=AZUL, height=3).pack(fill="x")

    eventos: queue.Queue = queue.Queue()
    cancelar = threading.Event()
    estado = {"rodando": False, "total": 0, "t0": None, "durs": [], "dur_total": 0.0,
              "dur_feita": 0.0, "dur_atual": 0.0, "p_atual": 0.0, "resultados": {}, "tarefas": [],
              "tamanhos": [], "log": NOME_LOG,
              "escolha": None}     # lista para escolher: {"chave", "raiz", "itens": [[origem, bytes, marcado]]}

    v_pasta = tk.StringVar()
    v_sub = tk.BooleanVar(value=True)
    v_modo = tk.StringVar(value="lado")
    v_saida = tk.StringVar()
    v_crf = tk.IntVar(value=CRF_PADRAO)
    v_preset = tk.IntVar(value=PRESET_PADRAO)
    tem_vmaf = ferramenta_vmaf() is not None
    v_vmaf = tk.BooleanVar(value=tem_vmaf)
    v_apagar = tk.BooleanVar(value=False)
    v_status = tk.StringVar(value="Escolha uma pasta para listar os vídeos, ou use "
                                  "\"Procurar vídeos grandes no PC\".")
    v_eta = tk.StringVar(value="será calculado ao iniciar o processamento")

    topo = ttk.Frame(janela, padding=(14, 12, 14, 8))
    topo.pack(fill="x")
    topo.columnconfigure(1, weight=1)

    def escolher(var):
        d = filedialog.askdirectory(initialdir=var.get() or None)
        if d:
            var.set(d)
            if var is v_pasta:
                carregar_lista()

    ttk.Label(topo, text="Pasta dos vídeos:", style="Rotulo.TLabel").grid(row=0, column=0, sticky="w")
    e_pasta = ttk.Entry(topo, textvariable=v_pasta)
    e_pasta.grid(row=0, column=1, sticky="ew", padx=8)
    e_pasta.bind("<Return>", lambda _: carregar_lista())
    e_pasta.bind("<FocusOut>", lambda _: carregar_lista(so_se_mudou=True))
    ttk.Button(topo, text="Procurar...", command=lambda: escolher(v_pasta)).grid(row=0, column=2)
    ttk.Checkbutton(topo, text="Incluir subpastas", variable=v_sub,
                    command=lambda: carregar_lista()).grid(row=1, column=1, sticky="w", padx=8, pady=(4, 8))
    ttk.Button(topo, text="Procurar vídeos grandes no PC...",
               command=lambda: abrir_busca()).grid(row=1, column=1, columnspan=2, sticky="e", pady=(4, 8))

    ttk.Label(topo, text="Salvar itens:", style="Rotulo.TLabel").grid(row=2, column=0, sticky="w")
    modos = ttk.Frame(topo)
    modos.grid(row=2, column=1, columnspan=2, sticky="w", padx=8)
    ttk.Radiobutton(modos, text=f'Ao lado do original (nome + "{SUFIXO}")', variable=v_modo,
                    value="lado", command=lambda: atualizar_modo()).pack(side="left")
    ttk.Radiobutton(modos, text="Em pasta separada (espelha as subpastas)", variable=v_modo,
                    value="pasta", command=lambda: atualizar_modo()).pack(side="left", padx=14)
    e_saida = ttk.Entry(topo, textvariable=v_saida)
    e_saida.grid(row=3, column=1, sticky="ew", padx=8, pady=4)
    b_saida = ttk.Button(topo, text="Procurar...", command=lambda: escolher(v_saida))
    b_saida.grid(row=3, column=2)

    ttk.Label(topo, text="Tempo estimado:", style="Rotulo.TLabel").grid(row=4, column=0, sticky="w", pady=(4, 0))
    ttk.Label(topo, textvariable=v_eta).grid(row=4, column=1, columnspan=2, sticky="w", padx=8, pady=(4, 0))

    def atualizar_modo():
        st = "normal" if v_modo.get() == "pasta" else "disabled"
        e_saida.configure(state=st)
        b_saida.configure(state=st)
        if v_modo.get() == "pasta" and not v_saida.get() and v_pasta.get():
            v_saida.set(v_pasta.get().rstrip("/\\") + "_RED")

    ajustes = ttk.Frame(topo)
    ajustes.grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))
    ttk.Label(ajustes, text="Qualidade (CRF):").pack(side="left")
    ttk.Spinbox(ajustes, from_=20, to=50, width=4, textvariable=v_crf).pack(side="left", padx=(4, 4))
    ttk.Label(ajustes, text="menor = mais qualidade", style="Dica.TLabel").pack(side="left", padx=(0, 18))
    ttk.Label(ajustes, text="Velocidade (preset):").pack(side="left")
    ttk.Spinbox(ajustes, from_=2, to=12, width=4, textvariable=v_preset).pack(side="left", padx=(4, 4))
    ttk.Label(ajustes, text="menor = mais lento", style="Dica.TLabel").pack(side="left", padx=(0, 18))
    ttk.Checkbutton(ajustes, text="Medir qualidade (VMAF)" if tem_vmaf else "VMAF indisponível neste ffmpeg",
                    variable=v_vmaf, state="normal" if tem_vmaf else "disabled").pack(side="left")

    def confirmar_apagar():
        """Ao marcar, pede confirmação (foco em "Não"); sem confirmação, volta a desmarcar."""
        if v_apagar.get() and not messagebox.askyesno(
                "Apagar arquivos originais", "Deseja realmente apagar os vídeos originais?",
                icon="warning", default="no", parent=janela):
            v_apagar.set(False)

    linha_apagar = ttk.Frame(topo)
    linha_apagar.grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))
    ttk.Checkbutton(linha_apagar, text="Apagar arquivos originais", variable=v_apagar,
                    command=confirmar_apagar).pack(side="left")
    ttk.Label(linha_apagar, text="somente depois de convertido com sucesso; não vai para a Lixeira",
              style="Dica.TLabel").pack(side="left", padx=(6, 0))

    # Rodapé antes da tabela, para a tabela ocupar o espaço que sobrar.
    base = ttk.Frame(janela, padding=(14, 8, 14, 12))
    base.pack(side="bottom", fill="x")
    base.columnconfigure(1, weight=1)

    meio = ttk.Frame(janela, padding=(14, 0))
    meio.pack(fill="both", expand=True)
    colunas = ("sel", "arquivo", "status", "original", "reduzido", "reducao", "vmaf")
    tabela = ttk.Treeview(meio, columns=colunas, show="headings", selectmode="none")
    for c, titulo, larg, anc in (("sel", MARCADO, 36, "center"),
                                 ("arquivo", "Arquivo", 330, "w"), ("status", "Situação", 230, "w"),
                                 ("original", "Original", 90, "e"), ("reduzido", "Reduzido", 90, "e"),
                                 ("reducao", "Redução", 95, "e"), ("vmaf", "VMAF", 95, "e")):
        tabela.heading(c, text=titulo, anchor=anc)
        tabela.column(c, width=larg, anchor=anc, stretch=(c == "arquivo"))
    tabela.tag_configure("total", background=AZUL_CLARO, foreground=AZUL_ESCURO, font=negrito)
    barra_rol = ttk.Scrollbar(meio, orient="vertical", command=tabela.yview)
    tabela.configure(yscrollcommand=barra_rol.set)
    tabela.pack(side="left", fill="both", expand=True)
    barra_rol.pack(side="right", fill="y")

    ttk.Label(base, text="Arquivo atual:").grid(row=0, column=0, sticky="w")
    p_arquivo = ttk.Progressbar(base, maximum=1.0)
    p_arquivo.grid(row=0, column=1, sticky="ew", padx=8, pady=3)
    ttk.Label(base, text="Total:").grid(row=1, column=0, sticky="w")
    p_total = ttk.Progressbar(base, maximum=1.0)
    p_total.grid(row=1, column=1, sticky="ew", padx=8, pady=3)
    ttk.Label(base, textvariable=v_status, wraplength=840, justify="left").grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
    botoes = ttk.Frame(base)
    botoes.grid(row=0, column=2, rowspan=3, sticky="e", padx=(10, 0))
    b_iniciar = ttk.Button(botoes, text="Iniciar", style="Primario.TButton")
    b_iniciar.pack(fill="x")
    b_cancelar = ttk.Button(botoes, text="Cancelar", state="disabled")
    b_cancelar.pack(fill="x", pady=(6, 0))
    ttk.Button(botoes, text="Registros...", command=lambda: abrir_registros(
        janela, [v_pasta.get().strip(), v_saida.get().strip(), Path.home() / "Documents"])
               ).pack(fill="x", pady=(6, 0))

    def atualizar_total():
        """Última linha da lista: totais das colunas e média do percentual de redução."""
        if not tabela.exists("total"):
            return
        res = list(estado["resultados"].values())
        feitos = [r for r in res if r.reducao is not None]
        original = sum(estado["tamanhos"])     # medido no início: o original pode ser apagado depois
        reduzido = sum(r.bytes_destino for r in feitos)
        media = f"média {sum(r.reducao for r in feitos) / len(feitos):.0f}%" if feitos else ""
        situacao = f"{len(feitos)} convertido(s) de {len(estado['tarefas'])}"
        notas = [r.vmaf for r in feitos if r.vmaf is not None]
        vmaf_medio = f"média {sum(notas) / len(notas):.1f}" if notas else ""
        tabela.item("total", values=("", f"TOTAL  ({len(estado['tarefas'])} arquivo(s))", situacao,
                                     tamanho(original), tamanho(reduzido) if feitos else "", media,
                                     vmaf_medio))

    def atualizar_eta():
        if not estado["rodando"] or estado["t0"] is None:
            return
        decorrido = time.monotonic() - estado["t0"]
        feito = estado["dur_feita"] + estado["dur_atual"] * estado["p_atual"]
        if estado["dur_total"] <= 0:
            v_eta.set("nada a converter")
        elif feito <= 0 or decorrido < 3:
            v_eta.set(f"calculando...  ({tempo(estado['dur_total'])} de vídeo para processar)")
        else:
            total = decorrido * estado["dur_total"] / feito
            v_eta.set(f"total ~{tempo(total)}   |   restante ~{tempo(total - decorrido)}   |   "
                      f"decorrido {tempo(decorrido)}")

    def trabalhador(raiz, tarefas, crf, preset, vmaf, apagar):
        durs = []
        for t in tarefas:                    # 1ª passada: quanto vídeo há para codificar
            if cancelar.is_set():
                break
            try:
                durs.append(duracao_a_converter(t))
            except Exception:
                durs.append(0.0)
        durs += [0.0] * (len(tarefas) - len(durs))
        eventos.put(("duracoes", durs))
        resultados = []
        for i, t in enumerate(tarefas):
            if cancelar.is_set():
                break
            eventos.put(("inicio", i))
            try:
                r = converter(t, crf, preset, lambda p, i=i: eventos.put(("progresso", i, p)), cancelar,
                              vmaf=vmaf, ao_etapa=lambda txt, i=i: eventos.put(("etapa", i, txt)),
                              apagar_original=apagar)
            except Exception as e:           # um arquivo ruim não derruba o lote
                r = Resultado(t, "Erro", str(e))
            if r.status != "Cancelado":
                registrar(raiz, r)
            resultados.append(r)
            eventos.put(("fim", i, r))
        eventos.put(("acabou", resultados))

    def ler_ajustes(pai=None):
        """Confere ffmpeg, pasta de saída, CRF e preset. Devolve (saida, crf, preset) ou None."""
        pai = pai or janela
        erro = verificar_ferramentas()
        if erro:
            messagebox.showerror("ffmpeg", erro, parent=pai)
            return None
        saida = None
        if v_modo.get() == "pasta":
            if not v_saida.get().strip():
                messagebox.showwarning("Pasta de saída", "Informe a pasta de saída na janela principal.",
                                       parent=pai)
                return None
            saida = Path(v_saida.get().strip())
        try:
            crf, preset = int(v_crf.get()), int(v_preset.get())
        except (tk.TclError, ValueError):
            messagebox.showwarning("Ajustes", "CRF e preset precisam ser números inteiros.", parent=pai)
            return None
        return saida, crf, preset

    def disparar(tarefas, raiz_log, crf, preset, raiz_nomes=None):
        """Preenche a tabela e inicia a conversão. raiz_nomes=None mostra o caminho completo."""
        tabela.delete(*tabela.get_children())
        for i, t in enumerate(tarefas):
            nome = str(t.origem)
            if raiz_nomes is not None:
                try:
                    nome = str(t.origem.relative_to(raiz_nomes.resolve()))
                except ValueError:
                    nome = t.origem.name
            tabela.insert("", "end", iid=str(i), values=("", nome, "Na fila", tamanho(t.origem.stat().st_size), "", "", ""))
        tabela.insert("", "end", iid="total", tags=("total",))
        cancelar.clear()
        estado["escolha"] = None                 # a tabela passa a mostrar o andamento
        tabela.heading("sel", text="")
        estado["tamanhos"] = [t.origem.stat().st_size for t in tarefas]
        estado.update(rodando=True, total=len(tarefas), t0=None, durs=[], dur_total=0.0, dur_feita=0.0,
                      dur_atual=0.0, p_atual=0.0, resultados={}, tarefas=tarefas,
                      log=NOME_LOG if raiz_nomes is not None else str(raiz_log / NOME_LOG))
        atualizar_total()
        p_arquivo["value"] = p_total["value"] = 0
        b_iniciar.configure(state="disabled")
        b_cancelar.configure(state="normal")
        v_status.set(f"Analisando {len(tarefas)} vídeo(s)...")
        v_eta.set("analisando os arquivos...")
        threading.Thread(target=trabalhador, args=(raiz_log, tarefas, crf, preset, bool(v_vmaf.get()),
                                                   bool(v_apagar.get())),
                         daemon=True).start()

    # ---- lista da pasta, com caixinha para escolher o que converter ----
    def chave_da_pasta():
        return (v_pasta.get().strip(), bool(v_sub.get()))

    def saida_atual():
        return Path(v_saida.get().strip()) if v_modo.get() == "pasta" and v_saida.get().strip() else None

    def atualizar_escolha():
        esc = estado["escolha"]
        if not esc or not tabela.exists("total"):
            return
        marc = [it for it in esc["itens"] if it[2]]
        tabela.heading("sel", text=MARCADO if marc and len(marc) == len(esc["itens"]) else DESMARCADO)
        tabela.item("total", values=("", f"TOTAL  ({len(esc['itens'])} arquivo(s))",
                                     f"{len(marc)} selecionado(s) para converter",
                                     tamanho(sum(it[1] for it in marc)), "", "", ""))
        v_status.set(f"{len(marc)} de {len(esc['itens'])} vídeo(s) selecionado(s). Clique na linha para "
                     "marcar ou desmarcar (no cabeçalho da caixinha, todos) e depois em Iniciar.")

    def carregar_lista(so_se_mudou=False):
        """Lista os vídeos da pasta escolhida, já marcados; os que já têm reduzido vêm desmarcados."""
        if estado["rodando"]:
            return False
        chave = chave_da_pasta()
        if so_se_mudou and ((estado["escolha"] or {}).get("chave") == chave or not chave[0]):
            return False
        raiz = Path(chave[0])
        if not chave[0] or not raiz.is_dir():
            if not so_se_mudou:
                messagebox.showwarning("Pasta", "Escolha uma pasta válida.")
            return False
        v_status.set("Carregando a lista de vídeos...")
        janela.update_idletasks()
        tabela.delete(*tabela.get_children())
        p_arquivo["value"] = p_total["value"] = 0
        v_eta.set("será calculado ao iniciar o processamento")
        itens = []
        for t in planejar(raiz, chave[1], saida_atual()):
            try:
                tam = t.origem.stat().st_size
            except OSError:
                continue
            feito = t.destino.exists() or ja_tem_reduzido(t.origem)
            try:
                nome = str(t.origem.relative_to(raiz.resolve()))
            except ValueError:
                nome = t.origem.name
            tabela.insert("", "end", iid=f"e{len(itens)}",
                          values=(DESMARCADO if feito else MARCADO, nome,
                                  "Já reduzido" if feito else "", tamanho(tam), "", "", ""))
            itens.append([t.origem, tam, not feito])
        estado["escolha"] = {"chave": chave, "raiz": raiz, "itens": itens}
        if not itens:
            tabela.heading("sel", text=DESMARCADO)
            v_status.set("Nenhum vídeo encontrado nessa pasta.")
            return True
        tabela.insert("", "end", iid="total", tags=("total",))
        atualizar_escolha()
        return True

    def clicar_tabela(ev):
        esc = estado["escolha"]
        if not esc or estado["rodando"]:
            return
        regiao = tabela.identify_region(ev.x, ev.y)
        if regiao == "heading" and tabela.identify_column(ev.x) == "#1":
            novo = not all(it[2] for it in esc["itens"])
            for i, it in enumerate(esc["itens"]):
                it[2] = novo
                tabela.set(f"e{i}", "sel", MARCADO if novo else DESMARCADO)
        elif regiao == "cell":
            iid = tabela.identify_row(ev.y)
            if not iid.startswith("e"):
                return
            it = esc["itens"][int(iid[1:])]
            it[2] = not it[2]
            tabela.set(iid, "sel", MARCADO if it[2] else DESMARCADO)
        else:
            return
        atualizar_escolha()

    tabela.bind("<Button-1>", clicar_tabela, add="+")

    def iniciar():
        raiz = Path(v_pasta.get().strip())
        if not v_pasta.get().strip() or not raiz.is_dir():
            messagebox.showwarning("Pasta", "Escolha uma pasta válida.")
            return
        esc = estado["escolha"]
        if not esc or esc["chave"] != chave_da_pasta():
            # Lista ainda não carregada (ou mostrando o resultado anterior): carrega para o usuário escolher.
            if carregar_lista() and estado["escolha"]["itens"]:
                v_status.set("Lista carregada. Confira os vídeos marcados e clique em Iniciar de novo.")
            return
        marcados = {it[0] for it in esc["itens"] if it[2]}
        if not marcados:
            messagebox.showwarning("Iniciar", "Nenhum vídeo está marcado na lista.")
            return
        ajustes_lidos = ler_ajustes()
        if not ajustes_lidos:
            return
        saida, crf, preset = ajustes_lidos
        tarefas = [t for t in planejar(raiz, v_sub.get(), saida) if t.origem in marcados]
        if not tarefas:
            messagebox.showwarning("Iniciar", "Os vídeos marcados não estão mais disponíveis na pasta.")
            return
        disparar(tarefas, raiz, crf, preset, raiz_nomes=raiz)

    # ---- janela "Procurar vídeos grandes" ----
    def abrir_busca():
        if estado["rodando"]:
            messagebox.showinfo("Procurar vídeos grandes", "Aguarde o fim da conversão em andamento.")
            return
        dlg = tk.Toplevel(janela)
        dlg.title("Procurar vídeos grandes")
        dlg.geometry("940x580")
        dlg.minsize(760, 440)
        dlg.configure(bg=FUNDO)
        dlg.transient(janela)

        b_onde = tk.StringVar(value="pc")
        b_pasta = tk.StringVar(value=v_pasta.get())
        b_min = tk.IntVar(value=TAMANHO_MINIMO_MB)
        b_status = tk.StringVar(value="Defina onde procurar e o tamanho mínimo e clique em Procurar.")
        b_sel = tk.StringVar(value="")
        fila: queue.Queue = queue.Queue()
        parar = threading.Event()
        busca = {"rodando": False, "tamanhos": {}, "omitidos": 0, "aberta": True, "raiz": None}

        cima = ttk.Frame(dlg, padding=(14, 12, 14, 6))
        cima.pack(fill="x")
        cima.columnconfigure(2, weight=1)
        discos = unidades_locais()
        nomes_discos = "  ".join(str(d).rstrip("\\/") or str(d) for d in discos)
        ttk.Label(cima, text="Onde procurar:", style="Rotulo.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(cima, text=f"Todo o computador ({nomes_discos})", variable=b_onde, value="pc",
                        command=lambda: atualizar_onde()).grid(row=0, column=1, columnspan=3, sticky="w", padx=8)
        ttk.Radiobutton(cima, text="Somente nesta pasta:", variable=b_onde, value="pasta",
                        command=lambda: atualizar_onde()).grid(row=1, column=1, sticky="w", padx=8, pady=4)
        e_bpasta = ttk.Entry(cima, textvariable=b_pasta)
        e_bpasta.grid(row=1, column=2, sticky="ew", pady=4)

        def escolher_bpasta():
            d = filedialog.askdirectory(initialdir=b_pasta.get() or None, parent=dlg)
            if d:
                b_pasta.set(d)

        b_bpasta = ttk.Button(cima, text="Procurar...", command=escolher_bpasta)
        b_bpasta.grid(row=1, column=3, padx=(8, 0))
        ttk.Label(cima, text="Tamanho mínimo:", style="Rotulo.TLabel").grid(row=2, column=0, sticky="w", pady=(4, 0))
        linha_min = ttk.Frame(cima)
        linha_min.grid(row=2, column=1, columnspan=3, sticky="w", padx=8, pady=(4, 0))
        ttk.Spinbox(linha_min, from_=1, to=1000000, increment=100, width=8, textvariable=b_min).pack(side="left")
        ttk.Label(linha_min, text="MB   (1 GB = 1024 MB)", style="Dica.TLabel").pack(side="left", padx=(6, 18))
        b_buscar = ttk.Button(linha_min, text="Procurar", style="Primario.TButton")
        b_buscar.pack(side="left")
        b_parar = ttk.Button(linha_min, text="Parar", state="disabled")
        b_parar.pack(side="left", padx=(6, 0))

        def atualizar_onde():
            st = "normal" if b_onde.get() == "pasta" else "disabled"
            e_bpasta.configure(state=st)
            b_bpasta.configure(state=st)

        baixo = ttk.Frame(dlg, padding=(14, 6, 14, 12))
        baixo.pack(side="bottom", fill="x")
        baixo.columnconfigure(1, weight=1)
        ttk.Label(baixo, textvariable=b_status, wraplength=880, justify="left").grid(row=0, column=0, columnspan=4, sticky="w")
        b_todos = ttk.Button(baixo, text="Selecionar todos")
        b_todos.grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Label(baixo, textvariable=b_sel, style="Dica.TLabel").grid(row=1, column=1, sticky="w", padx=10, pady=(8, 0))
        b_converter = ttk.Button(baixo, text="Converter selecionados", style="Primario.TButton", state="disabled")
        b_converter.grid(row=1, column=2, sticky="e", pady=(8, 0))
        ttk.Button(baixo, text="Fechar", command=lambda: fechar_busca()).grid(row=1, column=3, sticky="e", padx=(6, 0), pady=(8, 0))

        centro = ttk.Frame(dlg, padding=(14, 0))
        centro.pack(fill="both", expand=True)
        lista = ttk.Treeview(centro, columns=("arquivo", "tamanho", "data"), show="headings", selectmode="extended")
        for c, titulo, larg, anc in (("arquivo", "Arquivo", 620, "w"), ("tamanho", "Tamanho", 100, "e"),
                                     ("data", "Modificado em", 130, "e")):
            lista.heading(c, text=titulo, anchor=anc)
            lista.column(c, width=larg, anchor=anc, stretch=(c == "arquivo"))
        rol = ttk.Scrollbar(centro, orient="vertical", command=lista.yview)
        lista.configure(yscrollcommand=rol.set)
        lista.pack(side="left", fill="both", expand=True)
        rol.pack(side="right", fill="y")

        def inserir(arq, tam):
            try:
                data = datetime.fromtimestamp(arq.stat().st_mtime).strftime("%d/%m/%Y")
            except OSError:
                data = ""
            lista.insert("", "end", iid=str(arq), values=(str(arq), tamanho(tam), data))

        def atualizar_selecao(_=None):
            sel = lista.selection()
            total = sum(busca["tamanhos"].get(i, 0) for i in sel)
            b_sel.set(f"{len(sel)} selecionado(s)  -  {tamanho(total)}" if sel else
                      "Clique para selecionar; Ctrl ou Shift selecionam vários.")
            b_converter.configure(state="normal" if sel and not busca["rodando"] else "disabled")

        def varrer(raizes, minimo):
            def achou(arq, tam):
                if ja_tem_reduzido(arq):
                    fila.put(("omitido",))
                else:
                    fila.put(("achado", arq, tam))
            ultimo = [0.0]

            def andou(pasta):
                agora = time.monotonic()
                if agora - ultimo[0] > 0.15:
                    ultimo[0] = agora
                    fila.put(("pasta", pasta))
            try:
                procurar_grandes(raizes, minimo, parar, achou, andou)
            finally:
                fila.put(("fim",))

        def buscar():
            try:
                minimo_mb = int(b_min.get())
                if minimo_mb < 1:
                    raise ValueError
            except (tk.TclError, ValueError):
                messagebox.showwarning("Tamanho mínimo", "Informe o tamanho mínimo em MB (número inteiro).", parent=dlg)
                return
            if b_onde.get() == "pasta":
                raiz = Path(b_pasta.get().strip())
                if not b_pasta.get().strip() or not raiz.is_dir():
                    messagebox.showwarning("Pasta", "Escolha uma pasta válida.", parent=dlg)
                    return
                raizes, busca["raiz"] = [raiz], raiz
            else:
                raizes, busca["raiz"] = discos, None
            lista.delete(*lista.get_children())
            parar.clear()
            busca.update(rodando=True, tamanhos={}, omitidos=0)
            b_buscar.configure(state="disabled")
            b_parar.configure(state="normal")
            atualizar_selecao()
            b_status.set("Procurando...")
            threading.Thread(target=varrer, args=(raizes, minimo_mb * 1024 * 1024), daemon=True).start()

        def processar_busca():
            if not busca["aberta"]:
                return
            try:
                while True:
                    ev = fila.get_nowait()
                    if ev[0] == "achado":
                        busca["tamanhos"][str(ev[1])] = ev[2]
                        inserir(ev[1], ev[2])
                    elif ev[0] == "omitido":
                        busca["omitidos"] += 1
                    elif ev[0] == "pasta" and busca["rodando"]:
                        b_status.set(f"Procurando...  {len(busca['tamanhos'])} encontrado(s)  -  {ev[1]}")
                    elif ev[0] == "fim":
                        busca["rodando"] = False
                        b_buscar.configure(state="normal")
                        b_parar.configure(state="disabled")
                        ordem = sorted(busca["tamanhos"].items(), key=lambda par: (-par[1], par[0].lower()))
                        for pos, (iid, _) in enumerate(ordem):      # do maior para o menor
                            lista.move(iid, "", pos)
                        total = sum(busca["tamanhos"].values())
                        txt = ("Busca interrompida. " if parar.is_set() else "Busca concluída. ")
                        txt += (f"{len(ordem)} vídeo(s) com {int(b_min.get())} MB ou mais, somando {tamanho(total)}."
                                if ordem else "Nenhum vídeo desse tamanho foi encontrado.")
                        if busca["omitidos"]:
                            txt += f"  {busca['omitidos']} já reduzido(s) anteriormente não aparece(m) na lista."
                        b_status.set(txt)
                        atualizar_selecao()
            except queue.Empty:
                pass
            dlg.after(150, processar_busca)

        def converter_selecionados():
            sel = [Path(i) for i in lista.selection()]
            if not sel:
                return
            ajustes_lidos = ler_ajustes(dlg)
            if not ajustes_lidos:
                return
            saida, crf, preset = ajustes_lidos
            tarefas = planejar_arquivos([a for a in sel if a.is_file()], saida)
            if not tarefas:
                messagebox.showwarning("Converter", "Os arquivos selecionados não estão mais disponíveis.", parent=dlg)
                return
            onde = (f"na pasta {saida}" if saida else f'ao lado de cada original (nome + "{SUFIXO}")')
            if not messagebox.askyesno("Converter", f"Converter {len(tarefas)} vídeo(s)?\n\n"
                                       f"Os reduzidos serão gravados {onde}.\n" +
                                       ("Os originais serão APAGADOS depois de convertidos."
                                        if v_apagar.get() else "Os originais são mantidos."), parent=dlg):
                return
            if saida:
                raiz_log = saida
                saida.mkdir(parents=True, exist_ok=True)
            elif busca["raiz"] is not None:
                raiz_log = busca["raiz"]
            else:
                docs = Path.home() / "Documents"
                raiz_log = docs if docs.is_dir() else Path.home()
            fechar_busca()
            disparar(tarefas, raiz_log, crf, preset)

        def selecionar_todos():
            lista.selection_set(lista.get_children())

        def fechar_busca():
            parar.set()
            busca["aberta"] = False
            dlg.destroy()

        b_buscar.configure(command=buscar)
        b_parar.configure(command=parar.set)
        b_todos.configure(command=selecionar_todos)
        b_converter.configure(command=converter_selecionados)
        lista.bind("<<TreeviewSelect>>", atualizar_selecao)
        dlg.protocol("WM_DELETE_WINDOW", fechar_busca)
        atualizar_onde()
        atualizar_selecao()
        processar_busca()

    def pedir_cancelamento():
        cancelar.set()
        b_cancelar.configure(state="disabled")
        v_status.set("Cancelando...")

    def processar_eventos():
        try:
            while True:
                ev = eventos.get_nowait()
                if ev[0] == "duracoes":
                    estado.update(durs=ev[1], dur_total=sum(ev[1]), t0=time.monotonic())
                elif ev[0] == "inicio":
                    tabela.set(str(ev[1]), "status", "Convertendo...")
                    tabela.see(str(ev[1]))
                    p_arquivo["value"] = 0
                    estado.update(dur_atual=estado["durs"][ev[1]], p_atual=0.0)
                    v_status.set(f"Processando {ev[1] + 1} de {estado['total']}...")
                elif ev[0] == "etapa":
                    tabela.set(str(ev[1]), "status", ev[2])
                elif ev[0] == "progresso":
                    p_arquivo["value"] = ev[2]
                    p_total["value"] = (ev[1] + ev[2]) / estado["total"]
                    estado["p_atual"] = ev[2]
                elif ev[0] == "fim":
                    i, r = ev[1], ev[2]
                    situacao = r.status + (f" - {r.mensagem}" if r.mensagem else "")
                    tabela.set(str(i), "status", situacao)
                    if r.bytes_destino:
                        tabela.set(str(i), "reduzido", tamanho(r.bytes_destino))
                    if r.reducao is not None:
                        tabela.set(str(i), "reducao", f"{r.reducao:.0f}%")
                    if r.vmaf is not None:
                        tabela.set(str(i), "vmaf", f"{r.vmaf:.1f}")
                    p_total["value"] = (i + 1) / estado["total"]
                    estado["dur_feita"] += estado["durs"][i]
                    estado.update(dur_atual=0.0, p_atual=0.0)
                    estado["resultados"][i] = r
                    atualizar_total()
                elif ev[0] == "acabou":
                    decorrido = time.monotonic() - estado["t0"] if estado["t0"] else 0
                    estado["rodando"] = False
                    b_iniciar.configure(state="normal")
                    b_cancelar.configure(state="disabled")
                    tabela.see("total")
                    prefixo = "Cancelado. " if cancelar.is_set() else "Concluído. "
                    v_status.set(prefixo + resumo(ev[1]) + f"  Registro em {estado['log']}.")
                    v_eta.set(f"processamento {'cancelado após' if cancelar.is_set() else 'concluído em'} {tempo(decorrido)}")
        except queue.Empty:
            pass
        atualizar_eta()
        janela.after(200, processar_eventos)

    def fechar():
        if estado["rodando"] and not messagebox.askyesno(
                "Sair", "Há uma conversão em andamento. Cancelar e sair?"):
            return
        cancelar.set()
        janela.destroy()

    b_iniciar.configure(command=iniciar)
    b_cancelar.configure(command=pedir_cancelamento)
    janela.protocol("WM_DELETE_WINDOW", fechar)
    atualizar_modo()
    processar_eventos()
    janela.mainloop()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Reduz todos os vídeos de uma pasta (AV1), mantendo os originais.")
    ap.add_argument("--cli", metavar="PASTA", help="roda no terminal, sem janela, sobre a PASTA")
    ap.add_argument("--saida", metavar="PASTA", help="grava os reduzidos em pasta separada (padrão: ao lado do original)")
    ap.add_argument("--sem-subpastas", action="store_true", help="não entra nas subpastas")
    ap.add_argument("--sem-vmaf", action="store_true", help="não mede o índice VMAF (mais rápido)")
    ap.add_argument("--apagar-originais", action="store_true",
                    help="apaga cada original depois de convertido com sucesso (sem confirmação, não vai para a Lixeira)")
    ap.add_argument("--crf", type=int, default=CRF_PADRAO, help=f"qualidade, menor = melhor (padrão {CRF_PADRAO})")
    ap.add_argument("--preset", type=int, default=PRESET_PADRAO, help=f"velocidade, menor = mais lento (padrão {PRESET_PADRAO})")
    ap.add_argument("--versao", action="version", version=f"{TITULO} {VERSAO}")
    ap.add_argument("--registros", nargs="?", const="", metavar="PASTA_OU_CSV",
                    help="mostra os vídeos já processados (sem valor: todos os registros conhecidos)")
    ap.add_argument("--situacao", default="", help="com --registros: só esta situação (ex.: Erro)")
    args = ap.parse_args()
    if args.registros is not None:
        return imprimir_registros(args.registros, args.situacao)
    return rodar_cli(args) if args.cli else rodar_gui()


if __name__ == "__main__":
    sys.exit(main())
