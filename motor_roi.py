#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor de Otimização e Retenção com ROI (C&C) - reduz vídeos de segurança combinando
desconstrução temporal e compressão seletiva por região de interesse (ROI).

O vídeo é processado em duas passadas:

  1. Análise   - o ffmpeg entrega quadros reduzidos (5 por segundo, por padrão); o OpenCV acha
                 o que se move e o YOLOv8 (PyTorch ou OpenVINO) acha pessoas e veículos.
  2. Codificação - o vídeo é dividido em trechos "ativos" e "ociosos". Cada trecho é codificado
                 com a taxa de quadros do seu estado e com um mapa de quantização (filtro addroi
                 do ffmpeg): pessoas, veículos e movimento recebem mais bits do que o fundo.

Modos (--modo):
    retencao   mantém a linha do tempo inteira; trechos ociosos caem para 1 quadro/s (padrão)
    timelapse  trechos ociosos são acelerados (time-lapse adaptativo); sem áudio
    eventos    grava só os trechos com atividade; o índice fica no arquivo .json

Uso:
    python motor_roi.py                              -> abre a janela
    python motor_roi.py "C:\\Videos"                 -> processa a pasta no terminal
    python motor_roi.py video.mp4 --modo eventos     -> um arquivo, só os eventos
    python motor_roi.py --help                       -> lista as opções

Requisitos: Python 3.9+, numpy, opencv-python e ffmpeg/ffprobe no PATH (com libx265).
Opcional: ultralytics (YOLOv8) e openvino (aceleração Intel). Sem eles, o ROI usa só movimento.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

TITULO = "Motor de Otimização e Retenção com ROI"
VERSAO = "1.2.3"          # ao mudar, o GitHub Actions gera e publica o instalador novo
EXTENSOES = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".wmv", ".flv", ".webm",
             ".mpg", ".mpeg", ".ts", ".mts", ".m2ts", ".3gp", ".dav", ".h264", ".h265"}
SUFIXO = " - ROI"
NOME_LOG = "motor_roi_log.csv"
MODOS = ("retencao", "timelapse", "eventos")
MAX_ROIS = 12                       # regiões por trecho (o QSV aceita poucas dezenas)
MAX_OCIOSO = 120.0                  # trecho ocioso mais longo, em segundos

# Codificadores: nome no ffmpeg, se respeita o ROI do addroi, preset padrão e opção de qualidade.
CODIFICADORES = {
    "hevc":     ("libx265",   True,  "medium", "-crf"),
    "h264":     ("libx264",   True,  "medium", "-crf"),
    "hevc_qsv": ("hevc_qsv",  True,  "medium", "-q:v"),     # Intel Quick Sync (QP constante)
    "h264_qsv": ("h264_qsv",  True,  "medium", "-q:v"),
    "av1":      ("libsvtav1", False, "8",      "-crf"),     # o SVT-AV1 ignora o ROI do addroi
}
PESSOA, VEICULO, MOVIMENTO = "pessoa", "veiculo", "movimento"
CLASSES_COCO = {0: PESSOA, 1: VEICULO, 2: VEICULO, 3: VEICULO, 5: VEICULO, 7: VEICULO}
CORES = {PESSOA: "lime", VEICULO: "yellow", MOVIMENTO: "red"}

SEM_JANELA = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
PASTA_APP = Path(__file__).resolve().parent
FILTRO_DESENTRELACAR = "bwdif=mode=send_frame:parity=auto:deint=all"


def _achar(nome: str) -> str | None:
    """Procura a ferramenta primeiro junto do aplicativo (instalação) e depois no PATH."""
    exe = nome + (".exe" if os.name == "nt" else "")
    for pasta in (PASTA_APP / "ffmpeg", PASTA_APP, PASTA_APP / "ffmpeg" / "bin", PASTA_APP / "bin"):
        if (pasta / exe).is_file():
            return str(pasta / exe)
    return shutil.which(nome)


FFMPEG = _achar("ffmpeg") or "ffmpeg"
FFPROBE = _achar("ffprobe") or "ffprobe"


# --------------------------------------------------------------------------
# Hardware: processador, núcleos, memória e conversões simultâneas
# --------------------------------------------------------------------------
GB_POR_CONVERSAO = 3          # memória por vídeo: detector YOLO + trechos x265 em paralelo
NUCLEOS_POR_CONVERSAO_IDEAL = 4      # por vídeo processado ao mesmo tempo
NUCLEOS_POR_CONVERSAO_MIN = 2
SONDAGENS_SIMULTANEAS = 8     # ffprobe é quase todo espera de disco


@dataclass
class Hardware:
    cpu: str
    nucleos: list[list[int]]   # um item por núcleo físico: os processadores lógicos dele
    logicos: int
    ram_gb: float

    @property
    def fisicos(self) -> int:
        return len(self.nucleos)

    @property
    def maximo(self) -> int:
        """Teto de conversões simultâneas: >= 2 núcleos físicos e ~2 GB de RAM para cada uma."""
        por_cpu = self.fisicos // NUCLEOS_POR_CONVERSAO_MIN
        por_ram = int(self.ram_gb // GB_POR_CONVERSAO)
        return max(1, min(por_cpu, por_ram))

    @property
    def sugerido(self) -> int:
        """Valor inicial: cerca de 4 núcleos físicos por conversão."""
        return max(1, min(self.maximo, round(self.fisicos / NUCLEOS_POR_CONVERSAO_IDEAL)))

    def limitar(self, jobs: int) -> int:
        return max(1, min(self.maximo, int(jobs)))

    def grupos(self, jobs: int) -> list[list[int]]:
        """Divide os núcleos físicos entre as conversões (núcleo i -> conversão i % jobs).

        A distribuição alternada mistura núcleos de desempenho e de eficiência nos processadores
        híbridos (Intel 12ª geração em diante), que o Windows numera em sequência."""
        jobs = self.limitar(jobs)
        grupos: list[list[int]] = [[] for _ in range(jobs)]
        for i, logicos in enumerate(self.nucleos):
            grupos[i % jobs].extend(logicos)
        return [sorted(g) for g in grupos]

    def fisicos_em(self, cpus: list[int]) -> int:
        """Quantos núcleos físicos há num grupo de processadores lógicos."""
        conjunto = set(cpus)
        return max(1, sum(1 for n in self.nucleos if conjunto.issuperset(n)))

    def threads_por_conversao(self, jobs: int) -> int:
        return max(1, self.fisicos // self.limitar(jobs))

    def descricao(self) -> str:
        return (f"{self.cpu}  ·  {self.fisicos} núcleos físicos / {self.logicos} lógicos  ·  "
                f"{self.ram_gb:.0f} GB de RAM")


def _nucleos_windows() -> list[list[int]]:
    import ctypes

    class SLPI(ctypes.Structure):
        _fields_ = [("mascara", ctypes.c_size_t), ("relacao", ctypes.c_int),
                    ("_uniao", ctypes.c_ulonglong * 2)]

    k32 = ctypes.windll.kernel32
    tam = ctypes.c_ulong(0)
    k32.GetLogicalProcessorInformation(None, ctypes.byref(tam))
    if not tam.value:
        return []
    buf = (SLPI * (tam.value // ctypes.sizeof(SLPI)))()
    if not k32.GetLogicalProcessorInformation(buf, ctypes.byref(tam)):
        return []
    nucleos = []
    for e in buf:
        if e.relacao == 0:                         # RelationProcessorCore
            nucleos.append([b for b in range(64) if (e.mascara >> b) & 1])
    return [n for n in nucleos if n]


def _nucleos_linux() -> list[list[int]]:
    vistos: dict[str, list[int]] = {}
    base = Path("/sys/devices/system/cpu")
    disponiveis = os.sched_getaffinity(0) if hasattr(os, "sched_getaffinity") else None
    for cpu in sorted(base.glob("cpu[0-9]*"), key=lambda p: int(p.name[3:])):
        n = int(cpu.name[3:])
        if disponiveis is not None and n not in disponiveis:
            continue
        try:
            irmaos = (cpu / "topology" / "thread_siblings_list").read_text().strip()
        except OSError:
            irmaos = str(n)
        vistos.setdefault(irmaos, []).append(n)
    return list(vistos.values())


def _nome_cpu() -> str:
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return " ".join(str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).split())
        except Exception:
            pass
    else:
        try:
            for linha in Path("/proc/cpuinfo").read_text(errors="replace").splitlines():
                if linha.lower().startswith("model name"):
                    return " ".join(linha.split(":", 1)[1].split())
        except OSError:
            pass
    return "Processador"


def _ram_gb() -> float:
    try:
        if os.name == "nt":
            import ctypes

            class MEM(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("total", ctypes.c_ulonglong), ("livre", ctypes.c_ulonglong),
                            ("_pf", ctypes.c_ulonglong), ("_pfl", ctypes.c_ulonglong),
                            ("_v", ctypes.c_ulonglong), ("_vl", ctypes.c_ulonglong),
                            ("_ve", ctypes.c_ulonglong)]

            m = MEM()
            m.dwLength = ctypes.sizeof(MEM)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return m.total / 1024 ** 3
        else:
            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    except Exception:
        pass
    return 8.0


_HARDWARE: list[Hardware] = []


def hardware() -> Hardware:
    """Detecta (uma vez) processador, núcleos físicos e memória deste computador."""
    if not _HARDWARE:
        logicos = os.cpu_count() or 2
        try:
            nucleos = _nucleos_windows() if os.name == "nt" else _nucleos_linux()
        except Exception:
            nucleos = []
        if not nucleos:      # API indisponível: supõe 2 lógicos por núcleo (Hyper-Threading)
            nucleos = [list(range(i, min(i + 2, logicos))) for i in range(0, logicos, 2)]
        _HARDWARE.append(Hardware(_nome_cpu(), nucleos, logicos, _ram_gb()))
    return _HARDWARE[0]


_SVT: dict = {}


def versao_svt() -> int:
    """Versão principal da biblioteca SVT-AV1 do ffmpeg (0 se não for possível descobrir).

    Até a 1.x, o parâmetro lp= é o número de processadores lógicos; nas versões novas ele virou
    um "nível" de 0 a 6. Por isso o número de threads só é passado ao SVT-AV1 1.x; nas demais o
    limite vem da afinidade do processo (os núcleos que o Windows deixa o ffmpeg usar)."""
    if "major" not in _SVT:
        major = 0
        try:
            r = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "info", "-f", "lavfi",
                                "-i", "color=s=64x64:d=0.1", "-frames:v", "1", "-c:v", "libsvtav1",
                                "-f", "null", "-"], capture_output=True, text=True, errors="replace",
                               timeout=60, creationflags=SEM_JANELA)
            achado = re.search(r"SVT-AV1 Encoder Lib v?(\d+)\.", r.stderr)
            major = int(achado.group(1)) if achado else 0
        except (OSError, subprocess.SubprocessError):
            pass
        _SVT["major"] = major
    return _SVT["major"]


def _fixar_nucleos(proc: subprocess.Popen, cpus: list[int] | None) -> None:
    """Restringe o processo do ffmpeg aos processadores lógicos `cpus` (se der; nunca falha)."""
    if not cpus:
        return
    try:
        if os.name == "nt":
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            mascara = sum(1 << c for c in cpus if c < 64)
            if mascara:
                k32.SetProcessAffinityMask(ctypes.c_void_p(int(proc._handle)), mascara)
        elif hasattr(os, "sched_setaffinity"):
            os.sched_setaffinity(proc.pid, cpus)
    except Exception:
        pass


# Padrão visual C&C (derivado do logo)
MARCADO, DESMARCADO = "\u2611", "\u2610"     # caixinhas da coluna de seleção da lista
AZUL = "#0446A4"
AZUL_ESCURO = "#03357D"
AZUL_CLARO = "#DCE7F7"
FUNDO = "#F5F7FB"
BRANCO = "#FFFFFF"
TEXTO = "#1F2A3D"
CINZA = "#66728A"
LOGO_B64 = "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"
ICONE_B64 = "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAJSklEQVR42u2aeXBX1RXHz7n3/d4vvyX5ZcMkkI0kYIIkJCKLAUxEBzQqdqxDK45VsS4ztFOdalXaYmyLYF1mqg7ihkUpUEQrbqgYwhKIZckCIaySPfllJ/ntv/fuPf3jGRuQQHD8x5l3/vq9eTP33M+553zvuff9kIjgp2wMfuJmApgAJoAJYAKYACaACWACmAAmgAlgApgAP9CU4Q/fHfCJiCECoi+of1LZ9vmB9l2HOlu6fY4IxW7lNiuPi4ooyktcfGNmdkoUACxdUzv7iviSGeN0ITnDS5qBkKRwBgCt3f4tlW3l1R176nv6PKHICG63Kg6bkpbgvH1O6sLitCi75cCJ3ne2Nby05KrzAxgmiThjkuiNT06+8uGxutNnwBO8ZnZq6a/y8jJinDalbzBUdarv3+WNqz48uqAwJX9C3CsfHJs64eofED9j9r2Dob9vOLKurKG9zQMK3jE/c2FR2sTkSM6wucu3o7Zzxfq6ZW/XPrhg4u7DXUKefY1Cw0xKqQtBRB29/hse2waFb0PxWnXeutc/Pk7ns/d3NTlK1sP0t6Bo7Ybtp4lI04UctWm6IKIdtZ3jF/0HCtfAnH+m3/H+zlr39x15/NpvX94H096Eq9dc+/ttw18pZ8ceOGMdvf6Sx8tq6rttMbZQQFv9yIx7b8jShSQCxgABiEASIMJtc1Jz0lwlT5Q1NQ34QwIARn/JZMT+iwPtt/15pz+oW51qlF39aPnc3PHRmi4QEdGILxCB06a89JtpqZc5Hntpny+oXaCIKazJO1fsqanvccTZA2eCi+Zl3HtDVlgXDFHhyBARkTFUOHKGYU3kpLpWPzKThPQE9NFnjiTinB1tGlj01wp/SI9wWEJ+beWDBbnjo8OaUDjjDBkiQ+QMFY5CSl3IRxdOmjc7ta0nMCIAZ+zVj0+U72mxRkcEgrrNZV26KJcI+FA8zjGLwnRdzp82tmh2anuP79xZShKShDzv3R9KIX/36sG+3oDdbgl6w/mTx9x1fcZ3BX2uVg65f+quXMZGltGegdALm+vRpiCADGjFBYk5qS4iyS4oLETw6/mZvqAwnobHWOFM4YwxlMMghCDOcOv+jm2VrWqkKoUEXS66Nt2iMCkJR3DFGRLRzJz43IzYEWV0886mljaPYlMkERAU511GAEKCwkecPecIQLcUJl91eRwR8aH4cMYOnujdfbgrIcY2f/rYWKcqiYy5Gav59uffoCSGoAliNn5dfiIB4MW2JUR4/v6CEVdg64F2JGIAkghUnpsRgwA4Cll3OdTsVJfhwCjxJf/Y98tnKroHQo+/UXXl4o+rT/YhgCQiIs6xZzC461AnWbkgErpMirOnJzkRgMHFneWkuUYEqGs4QxYuCUgSMEyJdwDAaLYlKUlIaWgLY7j6oxOr1lStvC9/+eL8DX+c3dTU//S6w0bgjVQ61uzp9YQ4QyQAQYmxNmeEMmr5kiMCuPsDRroTgEXBCJXB6AgQv60zo1g27W7mNhUAw7rMz4yxxjv6PKHvCgYAOnr9pEvGERBAksPKVYUR0Wh8sbNT4iwAIQgQAAEBJYG8xC8HNJRDuiAh6d2vGlSFbatyh3r9SxZMFJJ0/dtXmpDDRycE+lGauRinSpKQABkKQQNebZiujGIdAIQkQZCfEc2svKzaXbq29ul3Dr2ydPYvitM5Q6vKI6ycM4yyq2CEHBAYegMirEtE+AEcZ2Ve1rgod7cPFIYAoMtGt2dadtwljYkAnGHRlMtWbaoLhPTnNh+tXX1T1rjIUFi89unJEy2DIU3kZcZekztGVbkmCBkAZ+7+gDegWy1WADm6ohthBWZNHgNCIgBDAk3UnO4f3qJeJHkAiEDh7PmN9S+8d/SqvASpCS2ob6lsM9T2phnjXt96au/R7nlTk7JTXBmJDggLAOIKc/cGG93eS+pEzg+waG66PSpCCEkEoPDy2i4piXN24ZUlAoYoJTGGS17e/9iLe5ffl//esiKbVdEJlr5Ztbe+R+Es3mXNTonc8pdrL0+Jsqr81lmpFNaNroSC+vaaTqMZu0ikCPACRZyXEXPH3HTh0xhnil35b11XRV0XQxQjD6wLYgzfr2jeXtNZXuNetbZ2cn5iUW5CeqJz2T1TKCx0SXev3OsN6i9uri+Znpw1NjKkCSJ46OYJrjEOoQlAAAU37mjSdcnYhRbBiNGTb1WPCCCJnr57SnKKK+gLqxYmhXzizRpNl5yfh4EAdCEtCvMF9UdXHegeCH62v4Mh+oK6L6Dpgh69fdL8WSnSr53q8Mx9+MtGt/dPd04O68aJh9ITnaV3T9G9YSSw2i1Vdd2vb/2GM2a0vd8PvKZLztln+9pXbqgbEYAkjYu3b1w2J9qp+gdDjihrZW3nAy9+LQkUzoT8VgR1QbogBLAo3OPXblm6vbFlsCAr1uvXIUJpaPWs39GscOQc3/lDYcEV8REc9x/pGvQLi8JVhSmceYO6kPLh27Lvu31SsD8IBKpdeeK1qi8PdqgWjgi6kGLIl7E5qha+u67r509+NSM7/qxeprS0dPh+JCSlJzivm5q053B3e6uHOSzVx3r3n+zPHR+TFGfjDBlDzpAzBMRPKlsXPFnW0uHd/Mzcwklj+r3hD8qbVKel4lDn2Dj7xOQoh01p6wmUVzRzl+1ow5lddV1j4+wbyxuPNw/MzBmjC7ng6mSPLiqq3UKQANiyp9VpVwoyY1QLZ0O+GMN+b3jFv+ru/duuW+akbXrqGpv1/+KJw0XG+G30tH2e0LMbjqz54pueTh/4NXBZbyxMKZwUnxATMejTjrV4yqrdTR2ehcVpzz40NTXeLqSUEu55rnL9luPAEDQRmxzFASZlRC+en1n67uGGU2dASNDEzDkpny2f63JaiAgBGcNPv25dvu5wZX03+HXQZUZO/M9mpWSnRimMtfT4q072lVe7nSp/6p4pD9w84Vzh/j6AUS6cMwDo6g+W17i/PtZz6HT/kcYBjz9styqJsbaMsZHFUxJuLUzOSIoEACEkIholuGlHU1m12xPQoh3q9Vcm3TorWeGsoy+w9svTx1sGCybE3n9jls3Kjc6ZAGjI1566rp21XQdP9dWe6mvq8nLO4iKtqQnOyenRJTOSSqaPs1q4lBIA2LAzAZp/NTABTAATwAQwAUwAE8AEMAFMABPABDABTICfpv0PaZASbFDvN9YAAAAASUVORK5CYII="


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


class Erro(Exception):
    """Falha esperada, com mensagem pronta para o usuário."""


class Cancelado(Exception):
    pass


# --------------------------------------------------------------------------
# Ajustes
# --------------------------------------------------------------------------
@dataclass
class Ajustes:
    modo: str = "retencao"
    codec: str = "hevc"
    preset: str = ""                 # vazio = padrão do codificador
    qp_fundo: int = 36               # qualidade de base (CRF/QP); maior = mais compressão
    qp_pessoa: int = 22
    qp_veiculo: int = 26
    qp_movimento: int = 30
    fps_ativo: float = 15.0          # 0 = mantém a taxa original
    fps_ocioso: float = 1.0
    fps_analise: float = 5.0
    lado_analise: int = 640          # maior lado do quadro analisado, em pixels
    janela: float = 4.0              # duração de cada trecho ativo (o ROI vale para o trecho)
    antes: float = 2.0               # segundos mantidos antes de cada atividade
    depois: float = 3.0              # segundos mantidos depois
    sensibilidade: float = 0.0015    # fração mínima do quadro para contar como movimento
    margem: float = 0.10             # folga em volta de cada região
    detector: str = "auto"           # auto | openvino | yolo | movimento
    modelo: str = "yolov8n.pt"
    confianca: float = 0.35
    dispositivo: str = ""            # repassado à ultralytics: cpu, 0, xpu, intel:gpu...
    audio: bool = True
    paralelo: int = 0                # 0 = automático
    mostrar_roi: bool = False        # desenha as regiões no vídeo (diagnóstico)
    calcular_hash: bool = True
    vmaf: bool = True                # mede a qualidade, se o ffmpeg tiver o filtro libvmaf
    apagar_original: bool = False    # apaga o original só depois de tudo gravado e conferido

    def validar(self) -> None:
        if self.modo not in MODOS:
            raise Erro(f"modo inválido: {self.modo}")
        if self.codec not in CODIFICADORES:
            raise Erro(f"codec inválido: {self.codec}")
        if not 0 < self.fps_ocioso <= 30 or self.fps_analise <= 0 or self.fps_ativo < 0:
            raise Erro("taxas de quadros inválidas")
        for nome in ("qp_fundo", "qp_pessoa", "qp_veiculo", "qp_movimento"):
            if not 0 <= getattr(self, nome) <= 51:
                raise Erro(f"{nome} deve ficar entre 0 e 51")
        if self.janela < 1:
            raise Erro("a janela deve ter pelo menos 1 segundo")

    def qp_da(self, classe: str) -> int:
        return {PESSOA: self.qp_pessoa, VEICULO: self.qp_veiculo}.get(classe, self.qp_movimento)


# --------------------------------------------------------------------------
# Estruturas
# --------------------------------------------------------------------------
Caixa = tuple  # (x0, y0, x1, y1), frações do quadro entre 0 e 1


@dataclass
class Amostra:
    t: float
    movimento: list = field(default_factory=list)          # caixas
    objetos: list = field(default_factory=list)            # (classe, caixa)


@dataclass
class Roi:
    classe: str
    x: float
    y: float
    w: float
    h: float


@dataclass
class Segmento:
    inicio: float
    fim: float
    ativo: bool
    rois: list = field(default_factory=list)
    inicio_saida: float = 0.0
    duracao_saida: float = 0.0
    quadros: int = 0                 # quadros codificados (estimativa; pesa a média do VMAF)
    vmaf: float | None = None
    vmaf_roi: float | None = None

    @property
    def duracao(self) -> float:
        return self.fim - self.inicio


@dataclass
class Resultado:
    origem: Path
    destino: Path
    status: str                      # Convertido | Sem ganho | Ignorado | Erro | Cancelado
    detalhe: str = ""
    tam_origem: int = 0
    tam_destino: int = 0
    duracao: float = 0.0
    duracao_saida: float = 0.0
    ativo: float = 0.0               # segundos com atividade
    eventos: int = 0
    segundos: float = 0.0            # tempo gasto
    vmaf: float | None = None        # média dos quadros mantidos, quadro inteiro
    vmaf_roi: float | None = None    # média só dentro das regiões de interesse

    @property
    def reducao(self) -> float | None:
        if self.tam_origem and self.tam_destino:
            return 100.0 * (1 - self.tam_destino / self.tam_origem)
        return None


# --------------------------------------------------------------------------
# ffmpeg / ffprobe
# --------------------------------------------------------------------------
class _Sinais:
    """Vários sinais de parada vistos como um só."""

    def __init__(self, *sinais):
        self.sinais = sinais

    def is_set(self) -> bool:
        return any(s.is_set() for s in self.sinais)


def _executar(cmd: list, cancelar=None, env=None, cpus: list[int] | None = None) -> tuple[int, str]:
    """Roda o comando até o fim (ou até o cancelamento). Devolve (código, texto de erro).
    `cpus` restringe o processo aos processadores lógicos reservados ao vídeo."""
    with tempfile.TemporaryFile() as erros:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=erros,
                                stdin=subprocess.DEVNULL, creationflags=SEM_JANELA, env=env)
        _fixar_nucleos(proc, cpus)
        while True:
            try:
                codigo = proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if cancelar is not None and cancelar.is_set():
                    proc.terminate()
                    proc.wait()
                    raise Cancelado()
        erros.seek(0)
        return codigo, erros.read().decode("utf-8", "replace")


def _ultima_linha(texto: str, codigo: int) -> str:
    linhas = [l for l in texto.strip().splitlines() if l.strip() and not l.startswith("Svt[")]
    return linhas[-1].strip() if linhas else f"ffmpeg saiu com código {codigo}"


def codificadores_disponiveis() -> set[str]:
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True,
                           errors="replace", creationflags=SEM_JANELA)
    except OSError:
        return set()
    return {l.split()[1] for l in r.stdout.splitlines() if len(l.split()) > 1}


def verificar_ferramentas(aj: Ajustes) -> None:
    for exe, nome in ((FFMPEG, "ffmpeg"), (FFPROBE, "ffprobe")):
        if not (shutil.which(exe) or Path(exe).is_file()):
            raise Erro(f"{nome} não foi encontrado. Instale o ffmpeg e coloque-o no PATH.")
    nome_ff = CODIFICADORES[aj.codec][0]
    if nome_ff not in codificadores_disponiveis():
        raise Erro(f"este ffmpeg não tem o codificador {nome_ff} (--codec {aj.codec}).")
    try:
        import cv2      # noqa: F401
        import numpy    # noqa: F401
    except ImportError as e:
        raise Erro(f"falta a biblioteca {e.name}. Rode: pip install -r requirements.txt")


_SONDAGENS: dict = {}
_trava_sondagens = threading.Lock()


def sondar(arquivo: Path) -> dict:
    """Como _sondar(), guardando o resultado enquanto o arquivo não mudar (a 1ª passada do lote
    sonda todos os vídeos e o processamento reaproveita)."""
    try:
        st = arquivo.stat()
        chave = (str(arquivo), st.st_size, st.st_mtime_ns)
    except OSError:
        return _sondar(arquivo)
    with _trava_sondagens:
        if chave in _SONDAGENS:
            return dict(_SONDAGENS[chave])
    info = _sondar(arquivo)
    if info:
        with _trava_sondagens:
            _SONDAGENS[chave] = dict(info)
    return info


def _sondar(arquivo: Path) -> dict:
    """Dados do primeiro fluxo de vídeo. {} se não houver vídeo legível."""
    # saída completa: os nomes das seções de -show_entries mudam entre versões do ffprobe
    cmd = [FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(arquivo)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           creationflags=SEM_JANELA)
        dados = json.loads(r.stdout or "{}")
    except (OSError, ValueError):
        return {}
    fluxos = dados.get("streams", [])
    video = next((f for f in fluxos if f.get("codec_type") == "video"), None)
    if not video or not video.get("width"):
        return {}

    def taxa(texto) -> float:
        try:
            a, b = str(texto).split("/")
            return float(a) / float(b) if float(b) else 0.0
        except (ValueError, ZeroDivisionError):
            return 0.0

    try:
        duracao = float(dados.get("format", {}).get("duration", 0))
    except (TypeError, ValueError):
        duracao = 0.0
    giro = video.get("tags", {}).get("rotate")
    for lado in video.get("side_data_list", []):
        giro = lado.get("rotation", giro)
    try:
        giro = int(float(giro or 0))
    except ValueError:
        giro = 0
    largura, altura = int(video["width"]), int(video["height"])
    if abs(giro) % 180 == 90:                    # o ffmpeg gira a imagem ao decodificar
        largura, altura = altura, largura
    fps = taxa(video.get("avg_frame_rate")) or taxa(video.get("r_frame_rate")) or 25.0
    return {"codec": video.get("codec_name", ""), "pix_fmt": video.get("pix_fmt", ""),
            "entrelacado": video.get("field_order", "") in ("tt", "bb", "tb", "bt"),
            "largura": largura, "altura": altura, "fps": fps, "duracao": duracao,
            "audio": any(f.get("codec_type") == "audio" for f in fluxos)}


# --------------------------------------------------------------------------
# Detecção: movimento (OpenCV) e objetos (YOLOv8)
# --------------------------------------------------------------------------
class DetectorMovimento:
    """Subtração de fundo (MOG2): devolve as caixas do que mudou em relação ao fundo aprendido."""

    def __init__(self, largura: int, altura: int, fracao_minima: float):
        import cv2
        self.cv2 = cv2
        self.fundo = cv2.createBackgroundSubtractorMOG2(history=200, varThreshold=32, detectShadows=False)
        self.nucleo = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        self.largura, self.altura = largura, altura
        self.area_minima = max(16.0, fracao_minima * largura * altura)
        self.vistos = 0

    def __call__(self, quadro) -> list:
        cv2 = self.cv2
        cinza = cv2.GaussianBlur(cv2.cvtColor(quadro, cv2.COLOR_BGR2GRAY), (5, 5), 0)
        mascara = self.fundo.apply(cinza)
        self.vistos += 1
        if self.vistos <= 2:                     # ainda aprendendo o fundo
            return []
        mascara = cv2.morphologyEx(mascara, cv2.MORPH_OPEN, self.nucleo)
        mascara = cv2.dilate(mascara, self.nucleo, iterations=2)
        contornos = cv2.findContours(mascara, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[-2]
        caixas = []
        for c in contornos:
            if cv2.contourArea(c) < self.area_minima:
                continue
            x, y, w, h = cv2.boundingRect(c)
            caixas.append((x / self.largura, y / self.altura,
                           (x + w) / self.largura, (y + h) / self.altura))
        return caixas


class DetectorYolo:
    """YOLOv8 pela biblioteca ultralytics; com OpenVINO, usa o modelo exportado (CPU/GPU/NPU Intel)."""

    def __init__(self, modelo: str, confianca: float, dispositivo: str, openvino: bool):
        from ultralytics import YOLO
        caminho = self._achar_modelo(modelo)
        self.descricao = f"YOLO ({Path(caminho).name})"
        if openvino and Path(caminho).suffix == ".pt":
            exportado = Path(caminho).with_name(Path(caminho).stem + "_openvino_model")
            if not exportado.is_dir():
                exportado = Path(YOLO(caminho).export(format="openvino", imgsz=640, verbose=False))
            caminho = str(exportado)
            self.descricao = f"YOLO + OpenVINO ({exportado.name})"
        self.modelo = YOLO(caminho, task="detect")
        self.opcoes = {"conf": confianca, "classes": sorted(CLASSES_COCO), "imgsz": 640,
                       "verbose": False}
        if dispositivo:
            self.opcoes["device"] = dispositivo

    @staticmethod
    def _achar_modelo(modelo: str) -> str:
        for pasta in (Path.cwd(), PASTA_APP):
            if (pasta / modelo).exists():
                return str(pasta / modelo)
        return modelo                             # a ultralytics baixa os modelos padrão pelo nome

    def __call__(self, quadro) -> list:
        caixas = self.modelo.predict(quadro, **self.opcoes)[0].boxes
        achados = []
        for classe, (x0, y0, x1, y1) in zip(caixas.cls.tolist(), caixas.xyxyn.tolist()):
            nome = CLASSES_COCO.get(int(classe))
            if nome:
                achados.append((nome, (x0, y0, x1, y1)))
        return achados


def criar_detector(aj: Ajustes, avisar):
    """Devolve o detector de objetos, ou None para trabalhar só com movimento."""
    if aj.detector == "movimento":
        return None
    try:
        import ultralytics  # noqa: F401
    except Exception as e:                        # ImportError ou falha ao carregar o PyTorch
        if aj.detector != "auto":
            raise Erro(f"não foi possível carregar a ultralytics ({e}). Rode: pip install ultralytics")
        avisar("Aviso: ultralytics não instalada; o ROI vai usar só a detecção de movimento.")
        return None
    usar_openvino = aj.detector == "openvino"
    if aj.detector == "auto":
        try:
            import openvino  # noqa: F401
            usar_openvino = True
        except Exception:
            pass
    try:
        return DetectorYolo(aj.modelo, aj.confianca, aj.dispositivo, usar_openvino)
    except Exception as e:
        if usar_openvino and aj.detector == "auto":
            avisar(f"Aviso: OpenVINO indisponível ({e}); usando o YOLO em PyTorch.")
            try:
                return DetectorYolo(aj.modelo, aj.confianca, aj.dispositivo, False)
            except Exception as e2:
                e = e2
        if aj.detector != "auto":
            raise Erro(f"não foi possível carregar o modelo {aj.modelo}: {e}")
        avisar(f"Aviso: YOLO indisponível ({e}); o ROI vai usar só a detecção de movimento.")
        return None


# --------------------------------------------------------------------------
# Passada 1: análise
# --------------------------------------------------------------------------
def _tamanho_analise(info: dict, lado: int) -> tuple[int, int]:
    escala = min(1.0, lado / max(info["largura"], info["altura"]))
    par = lambda v: max(2, int(round(v * escala / 2)) * 2)
    return par(info["largura"]), par(info["altura"])


def _tocam(a: Caixa, b: Caixa) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def analisar(origem: Path, info: dict, aj: Ajustes, detector, ao_progredir, cancelar,
             cpus: list[int] | None = None) -> list[Amostra]:
    """Percorre o vídeo em baixa resolução e anota, por amostra, o movimento e os objetos."""
    import numpy as np
    largura, altura = _tamanho_analise(info, aj.lado_analise)
    filtros = ([FILTRO_DESENTRELACAR] if info["entrelacado"] else []) + [
        f"fps={aj.fps_analise:g}", f"scale={largura}:{altura}:flags=fast_bilinear"]
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(origem),
           "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", ",".join(filtros),
           "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
    movimento = DetectorMovimento(largura, altura, aj.sensibilidade)
    a_cada = max(1, round(aj.fps_analise))        # sem movimento, o YOLO olha 1 vez por segundo
    total = max(1, int(info["duracao"] * aj.fps_analise))
    tamanho = largura * altura * 3
    amostras: list[Amostra] = []
    with tempfile.TemporaryFile() as erros:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=erros, stdin=subprocess.DEVNULL,
                                creationflags=SEM_JANELA, bufsize=tamanho)
        _fixar_nucleos(proc, cpus)
        try:
            while True:
                if cancelar.is_set():
                    raise Cancelado()
                dados = proc.stdout.read(tamanho)
                while dados and len(dados) < tamanho:
                    mais = proc.stdout.read(tamanho - len(dados))
                    if not mais:
                        break
                    dados += mais
                if len(dados) < tamanho:
                    break
                quadro = np.frombuffer(dados, np.uint8).reshape(altura, largura, 3)
                i = len(amostras)
                amostra = Amostra(i / aj.fps_analise, movimento(quadro))
                if detector is not None and (amostra.movimento or i % a_cada == 0):
                    for classe, caixa in detector(quadro):
                        # veículo parado é cenário: só conta se algo se move dentro dele
                        if classe == VEICULO and not any(_tocam(caixa, m) for m in amostra.movimento):
                            continue
                        amostra.objetos.append((classe, tuple(caixa)))
                amostras.append(amostra)
                ao_progredir(min(1.0, len(amostras) / total))
        finally:
            if proc.poll() is None:
                proc.terminate()
            proc.stdout.close()
            codigo = proc.wait()
        if not amostras:
            erros.seek(0)
            raise Erro("a análise não leu nenhum quadro: "
                       + _ultima_linha(erros.read().decode("utf-8", "replace"), codigo))
    return amostras


# --------------------------------------------------------------------------
# Linha do tempo: trechos ativos/ociosos e regiões de interesse
# --------------------------------------------------------------------------
def _area(c: Caixa) -> float:
    return max(0.0, c[2] - c[0]) * max(0.0, c[3] - c[1])


def _unir(a: Caixa, b: Caixa) -> Caixa:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _fundir(caixas: list) -> list:
    """Une as caixas que se tocam até não sobrar nenhuma sobreposição."""
    caixas = list(caixas)
    mudou = True
    while mudou:
        mudou = False
        for i in range(len(caixas)):
            for j in range(i + 1, len(caixas)):
                if _tocam(caixas[i], caixas[j]):
                    caixas[i] = _unir(caixas[i], caixas[j])
                    del caixas[j]
                    mudou = True
                    break
            if mudou:
                break
    return caixas


def _folga(c: Caixa, margem: float) -> Caixa:
    dx = max(0.01, (c[2] - c[0]) * margem)
    dy = max(0.01, (c[3] - c[1]) * margem)
    return (max(0.0, c[0] - dx), max(0.0, c[1] - dy), min(1.0, c[2] + dx), min(1.0, c[3] + dy))


def _coberta(c: Caixa, outras: list) -> bool:
    """True se a maior parte da caixa já está dentro de alguma das outras."""
    for o in outras:
        dentro = _area((max(c[0], o[0]), max(c[1], o[1]), min(c[2], o[2]), min(c[3], o[3])))
        if dentro >= 0.6 * _area(c):
            return True
    return False


def regioes_do_trecho(amostras: list[Amostra], aj: Ajustes) -> list[Roi]:
    """Regiões de interesse de um trecho: tudo o que foi visto nele, por prioridade de classe."""
    por_classe = {PESSOA: [], VEICULO: [], MOVIMENTO: []}
    for a in amostras:
        for classe, caixa in a.objetos:
            por_classe[classe].append(_folga(caixa, aj.margem))
        for caixa in a.movimento:
            por_classe[MOVIMENTO].append(_folga(caixa, aj.margem))
    for classe in por_classe:
        por_classe[classe] = _fundir(por_classe[classe])
    objetos = por_classe[PESSOA] + por_classe[VEICULO]
    por_classe[VEICULO] = [c for c in por_classe[VEICULO] if not _coberta(c, por_classe[PESSOA])]
    por_classe[MOVIMENTO] = [c for c in por_classe[MOVIMENTO] if not _coberta(c, objetos)]
    # movimento que toma o quadro quase todo (câmera mexendo, luz acendendo) não é região
    if sum(_area(c) for c in por_classe[MOVIMENTO]) > 0.5:
        por_classe[MOVIMENTO] = []
    for classe in (MOVIMENTO, VEICULO, PESSOA):  # se passar do limite, junta a classe em uma caixa só
        if sum(len(v) for v in por_classe.values()) <= MAX_ROIS:
            break
        if por_classe[classe]:
            unida = por_classe[classe][0]
            for c in por_classe[classe][1:]:
                unida = _unir(unida, c)
            por_classe[classe] = [unida]
    rois = []
    for classe in (PESSOA, VEICULO, MOVIMENTO):
        for c in por_classe[classe]:
            if c[2] - c[0] >= 0.02 and c[3] - c[1] >= 0.02:
                rois.append(Roi(classe, round(c[0], 4), round(c[1], 4),
                                round(c[2] - c[0], 4), round(c[3] - c[1], 4)))
    return rois


def montar_linha_do_tempo(amostras: list[Amostra], duracao: float, aj: Ajustes
                          ) -> tuple[list[Segmento], list[dict]]:
    """Classifica cada instante como ativo ou ocioso e corta o vídeo em trechos."""
    n, fa = len(amostras), aj.fps_analise
    duracao = max(duracao, n / fa)
    bruto = [bool(a.movimento) for a in amostras]
    # movimento em uma amostra isolada costuma ser ruído de compressão: exige duas seguidas
    firme = [bruto[i] and ((i > 0 and bruto[i - 1]) or (i + 1 < n and bruto[i + 1])) for i in range(n)]
    for a, ok in zip(amostras, firme):
        if not ok:
            a.movimento = []
    ativo = [firme[i] or any(c == PESSOA for c, _ in amostras[i].objetos) for i in range(n)]

    antes, depois = math.ceil(aj.antes * fa), math.ceil(aj.depois * fa)
    largo = [False] * n
    for i, a in enumerate(ativo):
        if a:
            for j in range(max(0, i - antes), min(n, i + depois + 1)):
                largo[j] = True
    # pausa curta entre duas atividades não compensa a troca de estado
    folga_minima, i = math.ceil(3.0 * fa), 0
    while i < n:
        if not largo[i]:
            j = i
            while j < n and not largo[j]:
                j += 1
            if 0 < i and j < n and j - i < folga_minima:
                largo[i:j] = [True] * (j - i)
            i = j
        else:
            i += 1

    faixas, i = [], 0                              # (indice inicial, indice final exclusivo, ativo)
    while i < n:
        j = i
        while j < n and largo[j] == largo[i]:
            j += 1
        faixas.append((i, j, largo[i]))
        i = j
    if not faixas:
        faixas = [(0, 0, True)]

    segmentos, eventos = [], []
    for k, (i, j, estado) in enumerate(faixas):
        t0 = i / fa
        t1 = duracao if k == len(faixas) - 1 else j / fa
        if t1 - t0 <= 0:
            continue
        if estado:
            vistos = amostras[i:j]
            eventos.append({
                "inicio": round(t0, 3), "fim": round(t1, 3),
                "pessoas": max((sum(c == PESSOA for c, _ in a.objetos) for a in vistos), default=0),
                "veiculos": max((sum(c == VEICULO for c, _ in a.objetos) for a in vistos), default=0),
                "movimento": any(a.movimento for a in vistos)})
        partes = max(1, math.ceil((t1 - t0) / (aj.janela if estado else MAX_OCIOSO) - 1e-9))
        for p in range(partes):
            a = t0 + (t1 - t0) * p / partes
            b = t0 + (t1 - t0) * (p + 1) / partes
            rois = []
            if estado:
                ia, ib = max(0, int(a * fa) - 1), min(n, math.ceil(b * fa) + 1)
                rois = regioes_do_trecho(amostras[ia:ib], aj)
            segmentos.append(Segmento(round(a, 6), round(b, 6), estado, rois))
    return segmentos, eventos


# --------------------------------------------------------------------------
# Passada 2: codificação por trechos
# --------------------------------------------------------------------------
def _fps_de_saida(info: dict, aj: Ajustes) -> float:
    """Taxa dos trechos ativos; 0 quando a taxa original é mantida."""
    if aj.fps_ativo <= 0 or aj.fps_ativo >= info["fps"] - 0.01:
        return 0.0
    return aj.fps_ativo


def filtros_de_tempo(seg: Segmento, info: dict, aj: Ajustes) -> tuple[list, float]:
    """Filtros que escolhem os quadros do trecho (os mesmos na codificação e na medição) e a taxa."""
    fps_ativo = _fps_de_saida(info, aj)
    filtros = [FILTRO_DESENTRELACAR] if info["entrelacado"] else []
    if seg.ativo:
        fps = fps_ativo or info["fps"]
        if fps_ativo:
            filtros.append(f"fps={fps_ativo:g}:start_time=0")
    else:
        fps = min(aj.fps_ocioso, info["fps"])
        filtros.append(f"fps={fps:g}:start_time=0")
    return filtros, fps


def comando_do_trecho(origem: Path, seg: Segmento, saida: Path, info: dict, aj: Ajustes,
                      threads: int = 0) -> list:
    """Comando ffmpeg de um trecho. `threads` > 0 limita as threads do codificador (vários vídeos
    ao mesmo tempo); 0 deixa o codificador usar o computador inteiro."""
    nome_ff, tem_roi, preset_padrao, opcao_qualidade = CODIFICADORES[aj.codec]
    fps_ativo = _fps_de_saida(info, aj)
    acelerar = aj.modo == "timelapse" and not seg.ativo
    filtros, fps = filtros_de_tempo(seg, info, aj)
    seg.quadros = max(1, round(seg.duracao * fps))
    fim = ["-t", f"{seg.duracao:.6f}"]
    if acelerar:                                   # time-lapse: os quadros do ocioso tocam na taxa ativa
        fps_toque = fps_ativo or info["fps"]
        quadros = max(1, round(seg.duracao * fps))
        filtros.append(f"setpts=N/({fps_toque:g}*TB)")
        fim = ["-frames:v", str(quadros), "-r", f"{fps_toque:g}"]
        seg.duracao_saida = quadros / fps_toque
    else:
        seg.duracao_saida = seg.duracao
    if tem_roi:
        for k, r in enumerate(seg.rois):
            desvio = min(0, aj.qp_da(r.classe) - aj.qp_fundo)
            if desvio:
                filtros.append(f"addroi=x=iw*{r.x}:y=ih*{r.y}:w=iw*{r.w}:h=ih*{r.h}"
                               f":qoffset={desvio}/51" + (":clear=1" if k == 0 else ""))
    if aj.mostrar_roi:
        for r in seg.rois:
            filtros.append(f"drawbox=x=iw*{r.x}:y=ih*{r.y}:w=iw*{r.w}:h=ih*{r.h}"
                           f":color={CORES[r.classe]}:t=3")
    dez_bits = any(b in info["pix_fmt"] for b in ("10", "12")) and aj.codec in ("hevc", "av1")
    pix = "nv12" if aj.codec.endswith("_qsv") else ("yuv420p10le" if dez_bits else "yuv420p")
    codificador = ["-c:v", nome_ff, "-preset", aj.preset or preset_padrao,
                   opcao_qualidade, str(aj.qp_fundo), "-pix_fmt", pix,
                   "-g", str(max(60, round(fps * 10)))]
    if aj.codec == "hevc":
        codificador += ["-x265-params", "log-level=error" + (f":pools={threads}" if threads else "")]
    elif aj.codec == "h264" and threads:
        codificador += ["-threads", str(threads)]
    elif aj.codec == "av1" and threads and versao_svt() == 1:
        codificador += ["-svtav1-params", f"lp={threads}"]
    if aj.codec != "av1":                          # cabeçalhos em cada quadro-chave: os trechos têm
        codificador += ["-bsf:v", "dump_extra=freq=keyframe"]   # parâmetros de taxa diferentes
    return [FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
            "-ss", f"{seg.inicio:.6f}", "-t", f"{seg.duracao:.6f}", "-i", str(origem),
            "-map", "0:v:0", "-an", "-sn", "-dn",
            *(["-vf", ",".join(filtros)] if filtros else []), *codificador, *fim, str(saida)]


def trechos_simultaneos(aj: Ajustes, cpus: list[int] | None) -> int:
    """Trechos codificados (ou medidos) ao mesmo tempo dentro de um vídeo.
    Sem `cpus`, vale o computador inteiro; com `cpus`, só os núcleos reservados a este vídeo."""
    if aj.paralelo:
        return aj.paralelo
    if aj.codec.endswith("_qsv"):
        return 2
    if cpus:
        return max(1, min(4, hardware().fisicos_em(cpus) // 2))
    return max(1, min(4, (os.cpu_count() or 2) // 4))     # comportamento de um vídeo por vez


def codificar_trechos(origem: Path, segmentos: list[Segmento], pasta: Path, info: dict, aj: Ajustes,
                      ao_progredir, cancelar, cpus: list[int] | None = None) -> list[Path]:
    """Codifica os trechos em paralelo. Devolve os arquivos na ordem da linha do tempo."""
    paralelo = trechos_simultaneos(aj, cpus)
    threads = max(1, len(cpus) // paralelo) if cpus else 0
    total = sum(s.duracao for s in segmentos) or 1.0
    feito, trava = [0.0], threading.Lock()
    ambiente = dict(os.environ, SVT_LOG="1")
    parar = threading.Event()
    sinal = _Sinais(cancelar, parar)

    def um(par):
        k, seg = par
        if sinal.is_set():
            raise Cancelado()
        saida = pasta / f"trecho_{k:06d}.mkv"
        codigo, erros = _executar(comando_do_trecho(origem, seg, saida, info, aj, threads), sinal,
                                  ambiente, cpus)
        if codigo != 0 or not saida.exists() or saida.stat().st_size == 0:
            raise Erro(f"trecho {k} ({seg.inicio:.1f}s): " + _ultima_linha(erros, codigo))
        with trava:
            feito[0] += seg.duracao
            ao_progredir(min(1.0, feito[0] / total))
        return saida

    fila = ThreadPoolExecutor(max_workers=paralelo)
    try:
        return list(fila.map(um, enumerate(segmentos)))
    except BaseException:
        parar.set()                                # um trecho falhou: interrompe os demais
        raise
    finally:
        fila.shutdown(wait=True, cancel_futures=True)


def _vmaf_do_trecho(exe: str, origem: Path, seg: Segmento, arquivo: Path, info: dict, aj: Ajustes,
                    fios: int, cancelar, recorte: str = "", cpus: list[int] | None = None) -> float | None:
    """VMAF de um trecho: compara cada quadro codificado com o mesmo quadro do original."""
    escolha = ",".join(filtros_de_tempo(seg, info, aj)[0])
    igualar = "settb=1/1000,setpts=N*40,format=yuv420p" + recorte   # mesma numeração nos dois lados
    grafo = (f"[0:v]{igualar}[d];[1:v]{escolha + ',' if escolha else ''}{igualar}[r];"
             f"[d][r]libvmaf=n_threads={fios}:shortest=1")
    cmd = [exe, "-hide_banner", "-nostdin", "-loglevel", "info", "-nostats", "-i", str(arquivo),
           "-ss", f"{seg.inicio:.6f}", "-t", f"{seg.duracao:.6f}", "-i", str(origem),
           "-lavfi", grafo, "-an", "-f", "null", "-"]
    codigo, texto = _executar(cmd, cancelar, cpus=cpus)
    achado = re.search(r"VMAF score:\s*([0-9.]+)", texto)
    return float(achado.group(1)) if codigo == 0 and achado else None


def _recorte_da_regiao(seg: Segmento, info: dict) -> str:
    """Filtro crop da região de maior prioridade do trecho; vazio se não houver uma de bom tamanho."""
    for r in seg.rois:
        if r.w * info["largura"] >= 96 and r.h * info["altura"] >= 96:
            par = lambda lado, fracao: f"trunc({lado}*{fracao}/2)*2"
            return f",crop={par('iw', r.w)}:{par('ih', r.h)}:{par('iw', r.x)}:{par('ih', r.y)}"
    return ""


def _media(pares: list) -> float | None:
    """Média ponderada de (valor, peso), ignorando os valores ausentes."""
    pares = [(v, p) for v, p in pares if v is not None]
    peso = sum(p for _, p in pares)
    return round(sum(v * p for v, p in pares) / peso, 2) if peso else None


def medir_vmaf(origem: Path, segmentos: list[Segmento], arquivos: list[Path], info: dict, aj: Ajustes,
               ao_progredir, cancelar, cpus: list[int] | None = None) -> dict:
    """Mede o VMAF de cada trecho (quadro inteiro e dentro da região) e devolve as médias.

    A comparação usa só os quadros mantidos: os quadros descartados na redução temporal não
    entram na conta, porque não existem na saída."""
    exe = ferramenta_vmaf()
    if cpus:
        paralelo = trechos_simultaneos(aj, cpus) if not aj.codec.endswith("_qsv") else 1
        fios = max(1, len(cpus) // paralelo)
    else:
        paralelo = aj.paralelo or max(1, min(4, (os.cpu_count() or 2) // 4))
        fios = max(1, (os.cpu_count() or 2) // paralelo)
    total = sum(s.duracao for s in segmentos) or 1.0
    feito, trava = [0.0], threading.Lock()

    def um(par):
        seg, arquivo = par
        if cancelar.is_set():
            raise Cancelado()
        seg.vmaf = _vmaf_do_trecho(exe, origem, seg, arquivo, info, aj, fios, cancelar, cpus=cpus)
        recorte = _recorte_da_regiao(seg, info)
        if recorte and seg.vmaf is not None:
            seg.vmaf_roi = _vmaf_do_trecho(exe, origem, seg, arquivo, info, aj, fios, cancelar, recorte, cpus)
        with trava:
            feito[0] += seg.duracao
            ao_progredir(min(1.0, feito[0] / total))

    fila = ThreadPoolExecutor(max_workers=paralelo)
    try:
        list(fila.map(um, zip(segmentos, arquivos)))
    finally:
        fila.shutdown(wait=True, cancel_futures=True)
    medidos = [s for s in segmentos if s.vmaf is not None]
    return {"geral": _media([(s.vmaf, s.quadros) for s in medidos]),
            "ativo": _media([(s.vmaf, s.quadros) for s in medidos if s.ativo]),
            "ocioso": _media([(s.vmaf, s.quadros) for s in medidos if not s.ativo]),
            "regioes": _media([(s.vmaf_roi, s.quadros) for s in medidos]),
            "minimo": round(min(s.vmaf for s in medidos), 2) if medidos else None,
            "trechos_medidos": len(medidos), "trechos": len(segmentos)}


def juntar(arquivos: list[Path], segmentos: list[Segmento], origem: Path, parcial: Path,
           com_audio: bool, pasta: Path, cancelar) -> None:
    """Concatena os trechos sem recodificar e traz o áudio e os metadados do original."""
    lista = pasta / "lista.txt"
    with open(lista, "w", encoding="utf-8") as f:
        f.write("ffconcat version 1.0\n")
        for arq, seg in zip(arquivos, segmentos):
            f.write(f"file '{arq.name}'\nduration {seg.duracao_saida:.6f}\n")

    def comando(audio: list) -> list:
        return [FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(lista), "-i", str(origem),
                "-map", "0:v:0", *(["-map", "1:a?"] if com_audio else []), "-map_metadata", "1",
                "-c:v", "copy", *(audio if com_audio else []), "-movflags", "+faststart", str(parcial)]

    codigo, erros = _executar(comando(["-c:a", "copy"]), cancelar)
    if codigo != 0 and com_audio:                 # o MP4 recusou o áudio original: recodifica em AAC
        codigo, erros = _executar(comando(["-c:a", "aac", "-b:a", "128k"]), cancelar)
    if codigo != 0:
        raise Erro("ao juntar os trechos: " + _ultima_linha(erros, codigo))


# --------------------------------------------------------------------------
# Um vídeo, do começo ao fim
# --------------------------------------------------------------------------
def _sha256(arquivo: Path) -> str:
    h = hashlib.sha256()
    with open(arquivo, "rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def apagar_original(origem: Path, res: Resultado, aj: Ajustes, manifesto: dict, caminho_manifesto: Path) -> None:
    """Apaga o original (sem Lixeira). Só é chamada com o reduzido gravado no destino, aprovado na
    conferência de duração, menor que o original e com o manifesto já gravado.
    Com as regiões desenhadas o reduzido é só um teste, e o original é mantido."""
    notas = [res.detalhe] if res.detalhe else []
    if aj.mostrar_roi:
        notas.append("original mantido: regiões desenhadas no vídeo (teste)")
    else:
        try:
            origem.unlink()
            notas.append("original apagado")
            manifesto["origem"]["apagado"] = True
            try:
                with open(caminho_manifesto, "w", encoding="utf-8") as f:
                    json.dump(manifesto, f, ensure_ascii=False, indent=1)
            except OSError:
                pass
        except OSError as e:
            notas.append(f"não foi possível apagar o original ({e.strerror or e})")
    res.detalhe = "; ".join(notas)


def processar(origem: Path, destino: Path, aj: Ajustes, detector, ao_progredir=None,
              ao_etapa=None, cancelar: threading.Event | None = None,
              cpus: list[int] | None = None) -> Resultado:
    """Analisa e codifica um vídeo. Grava o destino (.mp4) e o manifesto (.json) ao lado.
    `cpus` = processadores lógicos reservados a este vídeo (None = o computador inteiro)."""
    ao_progredir = ao_progredir or (lambda p: None)
    ao_etapa = ao_etapa or (lambda texto: None)
    cancelar = cancelar or threading.Event()
    comeco = time.time()
    res = Resultado(origem, destino, "Erro", tam_origem=origem.stat().st_size)
    if destino.exists():
        res.status, res.detalhe, res.tam_destino = "Ignorado", "arquivo reduzido já existe", destino.stat().st_size
        return res
    info = sondar(origem)
    if not info:
        res.status, res.detalhe = "Ignorado", "sem fluxo de vídeo legível"
        return res
    res.duracao = info["duracao"]
    destino.parent.mkdir(parents=True, exist_ok=True)
    parcial = destino.with_name(destino.stem + ".parcial.mp4")
    pasta = Path(tempfile.mkdtemp(prefix=".roi_", dir=destino.parent))
    try:
        ao_etapa("Analisando")
        medir = aj.vmaf and not aj.mostrar_roi and ferramenta_vmaf() is not None
        p_analise, p_codificar = (0.25, 0.45) if medir else (0.30, 0.65)
        amostras = analisar(origem, info, aj, detector, lambda p: ao_progredir(p_analise * p), cancelar, cpus)
        segmentos, eventos = montar_linha_do_tempo(amostras, info["duracao"], aj)
        res.eventos = len(eventos)
        res.ativo = sum(s.duracao for s in segmentos if s.ativo)
        if aj.modo == "eventos":
            segmentos = [s for s in segmentos if s.ativo]
            if not segmentos:
                res.status, res.detalhe = "Ignorado", "nenhuma atividade encontrada"
                return res
        ao_etapa("Codificando")
        arquivos = codificar_trechos(origem, segmentos, pasta, info, aj,
                                     lambda p: ao_progredir(p_analise + p_codificar * p), cancelar, cpus)
        ao_etapa("Juntando")
        com_audio = aj.audio and aj.modo == "retencao" and info["audio"]
        juntar(arquivos, segmentos, origem, parcial, com_audio, pasta, cancelar)

        posicao = 0.0
        for s in segmentos:
            s.inicio_saida = round(posicao, 6)
            posicao += s.duracao_saida
        conf = sondar(parcial)
        if not conf or abs(conf["duracao"] - posicao) > max(2.0, posicao * 0.02):
            raise Erro("o arquivo gerado não passou na conferência de duração")
        res.duracao_saida = conf["duracao"]
        qualidade = None
        if medir:
            ao_etapa("Medindo VMAF")
            qualidade = medir_vmaf(origem, segmentos, arquivos, info, aj,
                                   lambda p: ao_progredir(0.72 + 0.25 * p), cancelar, cpus)
            res.vmaf, res.vmaf_roi = qualidade["geral"], qualidade["regioes"]
        try:
            st = origem.stat()
            os.utime(parcial, (st.st_atime, st.st_mtime))    # preserva a data do original
        except OSError:
            pass
        parcial.replace(destino)
        res.tam_destino = destino.stat().st_size
        res.status = "Convertido" if res.tam_destino < res.tam_origem else "Sem ganho"
        if res.status == "Sem ganho":
            res.detalhe = "reduzido ficou maior ou igual ao original"

        ao_etapa("Registrando")
        manifesto = {
            "programa": f"{TITULO} {VERSAO}",
            "gerado_em": datetime.now().isoformat(timespec="seconds"),
            "origem": {"arquivo": origem.name, "bytes": res.tam_origem, "duracao": info["duracao"],
                       "codec": info["codec"], "largura": info["largura"], "altura": info["altura"],
                       "fps": round(info["fps"], 3)},
            "saida": {"arquivo": destino.name, "bytes": res.tam_destino, "duracao": res.duracao_saida,
                      "codec": CODIFICADORES[aj.codec][0]},
            "ajustes": asdict(aj),
            "detector": getattr(detector, "descricao", "somente movimento"),
            "resumo": {"segundos_ativos": round(res.ativo, 3),
                       "segundos_ociosos": round(max(0.0, info["duracao"] - res.ativo), 3),
                       "eventos": len(eventos), "reducao_percentual": round(res.reducao or 0.0, 2)},
            "qualidade_vmaf": qualidade,
            "eventos": eventos,
            "trechos": [{"inicio": s.inicio, "fim": s.fim, "inicio_na_saida": s.inicio_saida,
                         "estado": "ativo" if s.ativo else "ocioso", "vmaf": s.vmaf,
                         "vmaf_regiao": s.vmaf_roi, "rois": [asdict(r) for r in s.rois]}
                        for s in segmentos],
        }
        if aj.calcular_hash:
            manifesto["origem"]["sha256"] = _sha256(origem)
            manifesto["saida"]["sha256"] = _sha256(destino)
        manifesto["origem"]["apagado"] = False
        caminho_manifesto = destino.with_suffix(".json")
        with open(caminho_manifesto, "w", encoding="utf-8") as f:
            json.dump(manifesto, f, ensure_ascii=False, indent=1)
        if aj.apagar_original and res.status == "Convertido":
            apagar_original(origem, res, aj, manifesto, caminho_manifesto)
        ao_progredir(1.0)
    except Cancelado:
        res.status, res.detalhe = "Cancelado", ""
    except Erro as e:
        res.status, res.detalhe = "Erro", str(e)
    finally:
        shutil.rmtree(pasta, ignore_errors=True)
        try:
            parcial.unlink()
        except OSError:
            pass
        res.segundos = time.time() - comeco
    return res


# --------------------------------------------------------------------------
# Lote e registro
# --------------------------------------------------------------------------
class PoolDetectores:
    """Um detector por vídeo em processamento: o YOLO não pode ser usado por duas threads ao mesmo
    tempo. O primeiro é criado já (e define o aviso e a descrição); os demais, só quando outro
    vídeo começa e não há detector livre, até `maximo`."""

    def __init__(self, aj: Ajustes, avisar, maximo: int):
        self.aj, self.maximo = aj, max(1, maximo)
        self.livres: queue.Queue = queue.Queue()
        self.trava = threading.Lock()
        primeiro = criar_detector(aj, avisar)        # pode levantar Erro: sem detector não há lote
        self.descricao = getattr(primeiro, "descricao", "somente movimento")
        self.ativo = primeiro is not None
        self.criados = 1
        if self.ativo:
            self.livres.put(primeiro)
            if self.maximo > 1 and "OpenVINO" not in self.descricao:
                # PyTorch: as threads de cálculo são do processo inteiro; divide entre os vídeos.
                try:
                    import torch
                    torch.set_num_threads(max(1, hardware().fisicos // self.maximo))
                except Exception:
                    pass

    def pegar(self):
        if not self.ativo:
            return None
        try:
            return self.livres.get_nowait()
        except queue.Empty:
            pass
        with self.trava:
            criar = self.criados < self.maximo
            if criar:
                self.criados += 1
        if criar:
            try:
                novo = criar_detector(self.aj, lambda texto: None)
                if novo is not None:
                    return novo
            except Exception:
                pass
        return self.livres.get()                      # espera um detector ser devolvido

    def devolver(self, detector) -> None:
        if detector is not None:
            self.livres.put(detector)


def duracoes_a_processar(pares: list[tuple[Path, Path]], cancelar) -> list[float]:
    """Segundos de vídeo de cada par, sondando vários arquivos ao mesmo tempo (0 se já reduzido)."""
    def uma(par) -> float:
        origem, destino = par
        if cancelar.is_set() or destino.exists():
            return 0.0
        try:
            return sondar(origem).get("duracao", 0.0)
        except Exception:
            return 0.0
    with ThreadPoolExecutor(max_workers=SONDAGENS_SIMULTANEAS) as ex:
        return list(ex.map(uma, pares))


def processar_em_lote(pares: list[tuple[Path, Path]], raiz_log: Path, aj: Ajustes, jobs: int,
                      detectores: PoolDetectores, cancelar, ao_inicio=None, ao_progredir=None,
                      ao_etapa=None, ao_fim=None) -> list[Resultado]:
    """Processa até `jobs` vídeos ao mesmo tempo, cada um preso ao seu grupo de núcleos e com o
    seu detector. Os maiores começam primeiro. Os callbacks recebem o índice do par na lista.
    Devolve os resultados na ordem original (sem os vídeos que nem chegaram a começar)."""
    hw = hardware()
    jobs = hw.limitar(jobs)
    livres: queue.Queue = queue.Queue()
    for grupo in (hw.grupos(jobs) if jobs > 1 else [None]):   # 1 vídeo: o computador inteiro
        livres.put(grupo)
    ferramenta_vmaf()
    if aj.codec == "av1":
        versao_svt()
    resultados: dict[int, Resultado] = {}

    def tam(i: int) -> int:
        try:
            return pares[i][0].stat().st_size
        except OSError:
            return 0

    def rodar(i: int) -> None:
        if cancelar.is_set():
            return
        cpus = livres.get()
        detector = None
        try:
            if cancelar.is_set():
                return
            origem, destino = pares[i]
            if ao_inicio:
                ao_inicio(i)
            try:
                if not destino.exists():             # já reduzido: processar() só registra e sai
                    detector = detectores.pegar()
                r = processar(origem, destino, aj, detector,
                              (lambda p: ao_progredir(i, p)) if ao_progredir else None,
                              (lambda txt: ao_etapa(i, txt)) if ao_etapa else None, cancelar, cpus)
            except Exception as e:                   # um arquivo ruim não derruba o lote
                r = Resultado(origem, destino, "Erro", str(e))
            if r.status != "Cancelado":
                registrar(raiz_log, r, aj)
            resultados[i] = r
            if ao_fim:
                ao_fim(i, r)
        finally:
            detectores.devolver(detector)
            livres.put(cpus)

    ordem = sorted(range(len(pares)), key=tam, reverse=True)
    ex = ThreadPoolExecutor(max_workers=jobs)
    try:
        pendentes = {ex.submit(rodar, i) for i in ordem}
        while pendentes:     # espera em fatias curtas para o Ctrl+C funcionar também no Windows
            _, pendentes = wait(pendentes, timeout=0.5)
    except BaseException:
        cancelar.set()
        raise
    finally:
        ex.shutdown(wait=True)
    return [resultados[i] for i in range(len(pares)) if i in resultados]


def planejar(alvo: Path, subpastas: bool, pasta_saida: Path | None) -> tuple[Path, list[tuple[Path, Path]]]:
    """Lista (origem, destino) para um arquivo ou para os vídeos de uma pasta."""
    if alvo.is_file():
        raiz, arquivos = alvo.parent, [alvo]
    else:
        raiz = alvo
        arquivos = sorted(p for p in (alvo.rglob("*") if subpastas else alvo.iterdir())
                          if p.is_file() and p.suffix.lower() in EXTENSOES)
    pares = []
    for arq in arquivos:
        if arq.stem.endswith(SUFIXO) or ".parcial" in arq.name:
            continue
        if pasta_saida and pasta_saida in arq.parents:
            continue
        base = (pasta_saida / arq.parent.relative_to(raiz)) if pasta_saida else arq.parent
        pares.append((arq, base / (arq.stem + SUFIXO + ".mp4")))
    return raiz, pares


_trava_log = threading.Lock()


def registrar(raiz: Path, r: Resultado, aj: Ajustes) -> None:
    """Acrescenta uma linha ao CSV de controle (abre direto no Excel pt-BR).
    Seguro com vários vídeos terminando ao mesmo tempo."""
    with _trava_log:
        _registrar(raiz, r, aj)


def _registrar(raiz: Path, r: Resultado, aj: Ajustes) -> None:
    caminho = raiz / NOME_LOG
    novo = not caminho.exists()
    try:
        with open(caminho, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            if novo:
                w.writerow(["data", "arquivo", "status", "modo", "codec", "tamanho_original",
                            "tamanho_reduzido", "reducao_%", "duracao_s", "duracao_saida_s",
                            "ativo_s", "eventos", "tempo_gasto_s", "detalhe", "vmaf", "vmaf_regioes"])
            num = lambda v, casas=1: f"{v:.{casas}f}".replace(".", ",")
            w.writerow([datetime.now().strftime("%d/%m/%Y %H:%M:%S"), str(r.origem), r.status,
                        aj.modo, aj.codec, r.tam_origem, r.tam_destino or "",
                        num(r.reducao) if r.reducao is not None else "", num(r.duracao),
                        num(r.duracao_saida), num(r.ativo), r.eventos, num(r.segundos, 0), r.detalhe,
                        num(r.vmaf) if r.vmaf is not None else "",
                        num(r.vmaf_roi) if r.vmaf_roi is not None else ""])
        lembrar_log(caminho)
    except OSError:
        pass


def tamanho(n: int) -> str:
    valor = float(n)
    for unidade in ("B", "KB", "MB", "GB", "TB"):
        if valor < 1024 or unidade == "TB":
            return f"{valor:.0f} {unidade}" if unidade == "B" else f"{valor:.1f} {unidade}"
        valor /= 1024
    return f"{n} B"


def tempo(segundos: float) -> str:
    s = int(round(segundos))
    h, m, s = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}h{m:02d}min" if h else (f"{m}min{s:02d}s" if m else f"{s}s")


# Colunas da consulta: as mesmas da tela de processamento, com a data e hora na frente.
ID_APP = "motor-roi"
CHAVE_DATA, CHAVE_ARQUIVO, CHAVE_SAIDA, CHAVE_MSG = "data", "arquivo", None, "detalhe"
COLUNAS_REG = (("data", "Data/hora", 135, "w"), ("arquivo", "Arquivo", 250, "w"),
               ("status", "Situação", 200, "w"), ("original", "Original", 90, "e"),
               ("reduzido", "Reduzido", 90, "e"), ("reducao", "Redução", 100, "e"),
               ("atividade", "Atividade", 140, "e"), ("eventos", "Eventos", 70, "e"),
               ("vmaf", "VMAF", 70, "e"), ("vmaf_roi", "VMAF ROI", 85, "e"))


def registro_para_tela(r: dict) -> tuple[tuple, tuple]:
    """(valores exibidos, chaves de ordenação) de uma linha do CSV."""
    nome = Path(r.get("arquivo", "")).name
    st, det = r.get("status", ""), r.get("detalhe", "")
    feito = st in ("Convertido", "Sem ganho")
    orig, red = _reg_num(r.get("tamanho_original", "")), _reg_num(r.get("tamanho_reduzido", ""))
    pct = _reg_num(r.get("reducao_%", ""))
    dur, ativo, evs = (_reg_num(r.get(c, "")) for c in ("duracao_s", "ativo_s", "eventos"))
    vm, vr = _reg_num(r.get("vmaf", "")), _reg_num(r.get("vmaf_regioes", ""))
    valores = (_reg_data_txt(r), nome, st + (f" - {det}" if det else ""),
               tamanho(int(orig)) if orig else "", tamanho(int(red)) if red else "",
               f"{pct:.0f}%" if pct is not None else "",
               f"{tempo(ativo or 0)} de {tempo(dur or 0)}" if feito else "",
               int(evs) if feito and evs is not None else "",
               f"{vm:.1f}" if vm is not None else "", f"{vr:.1f}" if vr is not None else "")
    chaves = (r["_data"] or datetime.min, nome.lower(), st.lower(), orig or 0, red or 0, _reg_k(pct),
              (ativo or 0) if feito else -1.0, (evs or 0) if feito else -1.0, _reg_k(vm), _reg_k(vr))
    return valores, chaves


def total_registros(regs: list) -> tuple:
    """Última linha, como na tela de processamento: totais e médias."""
    feitos = [r for r in regs if r.get("status") in ("Convertido", "Sem ganho")]
    soma = lambda rs, c: sum(_reg_num(r.get(c, "")) or 0 for r in rs)
    media = _reg_media(feitos, "reducao_%")
    vm, vr = _reg_media(feitos, "vmaf"), _reg_media(feitos, "vmaf_regioes")
    return ("", f"TOTAL  ({len(regs)} registro(s))", f"{len(feitos)} convertido(s) de {len(regs)}",
            tamanho(int(soma(regs, "tamanho_original"))),
            tamanho(int(soma(feitos, "tamanho_reduzido"))) if feitos else "",
            f"média {media:.0f}%" if media is not None else "",
            tempo(soma(feitos, "ativo_s")) + " ativos" if feitos else "",
            int(soma(feitos, "eventos")) if feitos else "",
            f"{vm:.1f}" if vm is not None else "", f"{vr:.1f}" if vr is not None else "")


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
# Janela (Tkinter)
# --------------------------------------------------------------------------
MODOS_TELA = (("Retenção: tudo, ocioso com poucos quadros", "retencao"),
              ("Time-lapse: ocioso acelerado, sem áudio", "timelapse"),
              ("Eventos: só os trechos com atividade", "eventos"))
CODECS_TELA = (("H.265 (libx265)", "hevc"), ("H.264 (libx264)", "h264"),
               ("H.265 Intel Quick Sync", "hevc_qsv"), ("H.264 Intel Quick Sync", "h264_qsv"),
               ("AV1 (sem ROI)", "av1"))
DETECTORES_TELA = (("Automático", "auto"), ("YOLO + OpenVINO", "openvino"),
                   ("YOLO (PyTorch)", "yolo"), ("Só movimento", "movimento"))


class Dica:
    """Explicação que aparece ao parar o mouse sobre um campo da janela."""

    ATRASO_MS = 450

    def __init__(self, widgets, texto: str, largura: int = 430):
        import tkinter as tk
        self.tk, self.texto, self.largura = tk, texto, largura
        self.janela = None
        self.agendado = None
        for w in widgets if isinstance(widgets, (list, tuple)) else [widgets]:
            w.bind("<Enter>", self._agendar, add="+")
            w.bind("<Leave>", self._esconder, add="+")
            w.bind("<ButtonPress>", self._esconder, add="+")
            w.bind("<MouseWheel>", self._esconder, add="+")

    def _agendar(self, ev):
        self._esconder()
        w = ev.widget
        self.agendado = (w, w.after(self.ATRASO_MS, lambda: self._mostrar(w)))

    def _mostrar(self, w):
        self.agendado = None
        if self.janela is not None:
            return
        tk = self.tk
        self.janela = tk.Toplevel(w)
        self.janela.wm_overrideredirect(True)
        try:
            self.janela.attributes("-topmost", True)
        except tk.TclError:
            pass
        moldura = tk.Frame(self.janela, background=AZUL, padx=1, pady=1)
        moldura.pack()
        tk.Label(moldura, text=self.texto, justify="left", wraplength=self.largura, background=BRANCO,
                 foreground=TEXTO, padx=10, pady=7, font=("Segoe UI", 9)).pack()
        self.janela.update_idletasks()
        x = w.winfo_rootx()
        y = w.winfo_rooty() + w.winfo_height() + 4
        larg, alt = self.janela.winfo_reqwidth(), self.janela.winfo_reqheight()
        x = max(0, min(x, w.winfo_screenwidth() - larg - 8))
        if y + alt > w.winfo_screenheight() - 40:          # sem espaço embaixo: mostra em cima
            y = w.winfo_rooty() - alt - 4
        self.janela.wm_geometry(f"+{x}+{y}")

    def _esconder(self, _=None):
        if self.agendado is not None:
            w, ident = self.agendado
            self.agendado = None
            try:
                w.after_cancel(ident)
            except Exception:
                pass
        if self.janela is not None:
            self.janela.destroy()
            self.janela = None


NOTA_APAGAR = "somente depois de convertido com sucesso; não vai para a Lixeira"
NOTA_APAGAR_BLOQUEADO = "escolha os vídeos na lista para habilitar"

DICAS = {
    "modo": "Como o vídeo reduzido é montado:\n"
            "•  Retenção: grava tudo, com a mesma duração e o áudio do original. Nos trechos sem "
            "atividade, guarda poucos quadros por segundo. Indicado para guardar como evidência.\n"
            "•  Time-lapse: grava tudo, mas acelera os trechos sem atividade. O vídeo fica mais curto "
            "e sem áudio.\n"
            "•  Eventos: grava só os trechos com atividade. O vídeo fica bem mais curto e sem áudio.",
    "codec": "Codificador do vídeo reduzido:\n"
             "•  H.265 (libx265): padrão. Arquivos menores; usa o processador.\n"
             "•  H.264 (libx264): arquivos maiores, mas abre em qualquer aparelho ou player.\n"
             "•  Intel Quick Sync: usa a placa de vídeo Intel e é mais rápido. Ainda não foi validado "
             "em hardware.\n"
             "•  AV1: só reduz os quadros por segundo; o codificador SVT-AV1 ignora as regiões de "
             "interesse, então pessoas e fundo recebem a mesma qualidade.",
    "detector": "Como pessoas e veículos são encontrados nas imagens:\n"
                "•  Automático: usa o YOLO com OpenVINO, se estiver instalado; senão, o YOLO em PyTorch; "
                "sem o YOLO, só a detecção de movimento.\n"
                "•  YOLO + OpenVINO: mais rápido em processadores e placas de vídeo Intel.\n"
                "•  YOLO (PyTorch): reconhece pessoas e veículos em qualquer computador.\n"
                "•  Só movimento: não reconhece pessoas nem veículos. Tudo o que se mexe vira região de "
                "interesse, e uma pessoa parada não é percebida.",
    "qp": "Qualidade de cada tipo de região, de 10 a 51: quanto MENOR o número, mais nítida a imagem e "
          "maior o arquivo.\n"
          "O Fundo é a base do quadro inteiro; pessoas, veículos e movimento recebem mais qualidade que "
          "ele. Um valor maior que o do Fundo não tem efeito.",
    "pessoa": "Qualidade das áreas com pessoas (padrão 22).\n"
              "É a região mais importante para reconhecer alguém. Diminua para rostos e roupas mais "
              "nítidos; aumente para economizar espaço.",
    "veiculo": "Qualidade das áreas com veículos em movimento (padrão 26).\n"
               "Veículo estacionado é tratado como cenário e fica com a qualidade do Fundo. Para ler "
               "placas, diminua este valor.",
    "movimento": "Qualidade das outras áreas com movimento que não são pessoa nem veículo (padrão 30): "
                 "animais, portões, objetos.\n"
                 "Sem o YOLO (detector Só movimento), todo movimento usa este valor.",
    "fundo": "Qualidade do restante da imagem (padrão 36): paredes, chão, céu, cenário parado.\n"
             "É o que mais pesa no tamanho do arquivo: aumentar este número reduz bastante o vídeo; "
             "diminuir deixa o cenário mais nítido.",
    "fps": "Quantos quadros por segundo o vídeo reduzido guarda em cada situação. Menos quadros, "
           "arquivo menor.",
    "fps_ativo": "Quadros por segundo nos trechos com atividade (movimento ou pessoas). Padrão 15.\n"
                 "0 mantém a taxa original da câmera. Para movimentos rápidos ou leitura de placas, use "
                 "valores maiores.",
    "fps_ocioso": "Quadros por segundo nos trechos sem atividade. Padrão 1 (uma imagem por segundo).\n"
                  "No Time-lapse, define a aceleração: com 15 quadros/s na atividade e 1 sem atividade, os "
                  "trechos parados passam 15 vezes mais rápido.\n"
                  "No modo Eventos não se aplica: os trechos sem atividade são descartados.",
    "vmaf": "Compara o vídeo reduzido com o original e dá notas de 0 a 100 (quanto mais perto de 100, "
            "mais parecido com o original): uma para o quadro inteiro e outra só dentro das regiões de "
            "interesse.\n"
            "As notas aparecem na tabela, no registro e no arquivo .json. O processamento fica mais demorado.",
    "apagar": "Apaga cada vídeo original depois que o reduzido e o manifesto .json foram gravados, "
              "conferidos e o reduzido ficou menor que ele. Vídeos com erro, cancelados ou sem ganho mantêm "
              "o original; com \"Desenhar as regiões\", nada é apagado.\n"
              "O arquivo é apagado direto, sem passar pela Lixeira.\n"
              "Fica disponível depois de escolher os vídeos na lista e volta a ficar desmarcado a cada lote.",
    "regioes": "Desenha retângulos em volta das regiões detectadas (pessoas, veículos, movimento) no vídeo "
               "reduzido, para conferir se a detecção está acertando.\n"
               "Use só para teste: os retângulos ficam gravados no vídeo, e a medição VMAF não é feita.",
}


def resumo(resultados: list[Resultado]) -> str:
    feitos = [r for r in resultados if r.status == "Convertido"]
    erros = sum(r.status == "Erro" for r in resultados)
    texto = f"{len(feitos)} convertido(s) de {len(resultados)}"
    if feitos:
        antes, depois = sum(r.tam_origem for r in feitos), sum(r.tam_destino for r in feitos)
        texto += f": {tamanho(antes)} -> {tamanho(depois)} ({100 * (1 - depois / antes):.0f}% menor)"
    return texto + (f"; {erros} com erro" if erros else "") + "."


def rodar_gui() -> int:
    for nome in ("stdout", "stderr"):            # aberto pelo pythonw não há terminal: as bibliotecas
        if getattr(sys, nome) is None:           # que escrevem nele (ultralytics) falhariam
            setattr(sys, nome, open(os.devnull, "w", encoding="utf-8"))
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
    janela.geometry("1180x880")
    janela.minsize(1000, 700)
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
    est.configure("TCombobox", fieldbackground=BRANCO, arrowcolor=AZUL, background=BRANCO)
    est.map("TCombobox", fieldbackground=[("readonly", BRANCO)], foreground=[("readonly", TEXTO)],
            selectbackground=[("readonly", BRANCO)], selectforeground=[("readonly", TEXTO)])
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
    tk.Label(titulos, text="Redução de vídeos de segurança por atividade e região de interesse, "
                           "mantendo os originais", bg=BRANCO, fg=CINZA).pack(anchor="w")
    tk.Frame(janela, bg=AZUL, height=3).pack(fill="x")

    eventos: queue.Queue = queue.Queue()
    cancelar = threading.Event()
    estado = {"rodando": False, "total": 0, "t0": None, "durs": [], "dur_total": 0.0,
              "dur_feita": 0.0, "andamento": {}, "etapas": {}, "n_fim": 0, "jobs": 1,
              "resultados": {}, "pares": [], "tamanhos": [],
              "escolha": None}     # lista para escolher: {"chave", "raiz", "itens": [[origem, bytes, marcado]]}

    padrao = Ajustes()
    disponiveis = codificadores_disponiveis()
    codecs = [c for c in CODECS_TELA if not disponiveis or CODIFICADORES[c[1]][0] in disponiveis] \
        or list(CODECS_TELA)
    v_pasta = tk.StringVar()
    v_sub = tk.BooleanVar(value=True)
    v_onde = tk.StringVar(value="lado")
    v_saida = tk.StringVar()
    v_modo = tk.StringVar(value=MODOS_TELA[0][0])
    v_codec = tk.StringVar(value=codecs[0][0])
    v_detector = tk.StringVar(value=DETECTORES_TELA[0][0])
    v_qp = {nome: tk.IntVar(value=getattr(padrao, "qp_" + nome))
            for nome in ("pessoa", "veiculo", "movimento", "fundo")}
    v_fps_ativo = tk.StringVar(value=f"{padrao.fps_ativo:g}")
    v_fps_ocioso = tk.StringVar(value=f"{padrao.fps_ocioso:g}")
    tem_vmaf = ferramenta_vmaf() is not None
    # Uma opção ou outra, nunca as duas: desenhar as regiões no vídeo impede medir o VMAF.
    v_verificacao = tk.StringVar(value="vmaf")
    v_apagar = tk.BooleanVar(value=False)
    hw = hardware()
    v_jobs = tk.IntVar(value=hw.sugerido)
    v_jobs_dica = tk.StringVar()
    v_status = tk.StringVar(value="Escolha uma pasta para listar os vídeos.")
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

    ttk.Label(topo, text="Salvar itens:", style="Rotulo.TLabel").grid(row=2, column=0, sticky="w")
    onde = ttk.Frame(topo)
    onde.grid(row=2, column=1, columnspan=2, sticky="w", padx=8)
    ttk.Radiobutton(onde, text=f'Ao lado do original (nome + "{SUFIXO}")', variable=v_onde,
                    value="lado", command=lambda: atualizar_onde()).pack(side="left")
    ttk.Radiobutton(onde, text="Em pasta separada (espelha as subpastas)", variable=v_onde,
                    value="pasta", command=lambda: atualizar_onde()).pack(side="left", padx=14)
    e_saida = ttk.Entry(topo, textvariable=v_saida)
    e_saida.grid(row=3, column=1, sticky="ew", padx=8, pady=4)
    b_saida = ttk.Button(topo, text="Procurar...", command=lambda: escolher(v_saida))
    b_saida.grid(row=3, column=2)

    ttk.Label(topo, text="Tempo estimado:", style="Rotulo.TLabel").grid(row=4, column=0, sticky="w", pady=(4, 0))
    ttk.Label(topo, textvariable=v_eta).grid(row=4, column=1, columnspan=2, sticky="w", padx=8, pady=(4, 0))

    def atualizar_onde():
        st = "normal" if v_onde.get() == "pasta" else "disabled"
        e_saida.configure(state=st)
        b_saida.configure(state=st)
        if v_onde.get() == "pasta" and not v_saida.get() and v_pasta.get():
            v_saida.set(v_pasta.get().rstrip("/\\") + "_ROI")

    def caixa(pai, variavel, opcoes, largura):
        c = ttk.Combobox(pai, textvariable=variavel, values=[o[0] for o in opcoes],
                         state="readonly", width=largura)
        c.bind("<<ComboboxSelected>>", lambda _: c.selection_clear())
        return c

    linha1 = ttk.Frame(topo)
    linha1.grid(row=5, column=0, columnspan=3, sticky="w", pady=(12, 0))
    rotulo = ttk.Label(linha1, text="Modo:", style="Rotulo.TLabel")
    rotulo.pack(side="left")
    campo = caixa(linha1, v_modo, MODOS_TELA, 38)
    campo.pack(side="left", padx=(6, 18))
    Dica([rotulo, campo], DICAS["modo"])
    rotulo = ttk.Label(linha1, text="Codec:", style="Rotulo.TLabel")
    rotulo.pack(side="left")
    campo = caixa(linha1, v_codec, codecs, 22)
    campo.pack(side="left", padx=(6, 18))
    Dica([rotulo, campo], DICAS["codec"])
    rotulo = ttk.Label(linha1, text="Detector:", style="Rotulo.TLabel")
    rotulo.pack(side="left")
    campo = caixa(linha1, v_detector, DETECTORES_TELA, 17)
    campo.pack(side="left", padx=(6, 0))
    Dica([rotulo, campo], DICAS["detector"])

    linha2 = ttk.Frame(topo)
    linha2.grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))
    rotulo = ttk.Label(linha2, text="Qualidade (QP):", style="Rotulo.TLabel")
    rotulo.pack(side="left")
    Dica(rotulo, DICAS["qp"])
    for nome, texto in (("pessoa", "Pessoa"), ("veiculo", "Veículo"), ("movimento", "Movimento"),
                        ("fundo", "Fundo")):
        rotulo = ttk.Label(linha2, text=texto)
        rotulo.pack(side="left", padx=(10, 0))
        campo = ttk.Spinbox(linha2, from_=10, to=51, width=4, textvariable=v_qp[nome])
        campo.pack(side="left", padx=(4, 0))
        Dica([rotulo, campo], DICAS[nome])
    rotulo = ttk.Label(linha2, text="menor = mais qualidade", style="Dica.TLabel")
    rotulo.pack(side="left", padx=(10, 0))
    Dica(rotulo, DICAS["qp"])

    linha3 = ttk.Frame(topo)
    linha3.grid(row=7, column=0, columnspan=3, sticky="w", pady=(8, 0))
    rotulo = ttk.Label(linha3, text="Quadros por segundo:", style="Rotulo.TLabel")
    rotulo.pack(side="left")
    Dica(rotulo, DICAS["fps"])
    rotulo = ttk.Label(linha3, text="Com atividade")
    rotulo.pack(side="left", padx=(10, 0))
    campo = ttk.Spinbox(linha3, from_=0, to=60, width=4, textvariable=v_fps_ativo)
    campo.pack(side="left", padx=(4, 0))
    nota = ttk.Label(linha3, text="0 = taxa original", style="Dica.TLabel")
    nota.pack(side="left", padx=(6, 0))
    Dica([rotulo, campo, nota], DICAS["fps_ativo"])
    rotulo = ttk.Label(linha3, text="Sem atividade")
    rotulo.pack(side="left", padx=(14, 0))
    campo = ttk.Spinbox(linha3, from_=0.2, to=10, increment=0.5, width=4, textvariable=v_fps_ocioso)
    campo.pack(side="left", padx=(4, 0))
    Dica([rotulo, campo], DICAS["fps_ocioso"])

    linha4 = ttk.Frame(topo)
    linha4.grid(row=8, column=0, columnspan=3, sticky="w", pady=(8, 0))
    campo = ttk.Radiobutton(linha4, text="Medir qualidade (VMAF)" if tem_vmaf else "VMAF indisponível neste ffmpeg",
                            variable=v_verificacao, value="vmaf")
    campo.pack(side="left")
    nota = ttk.Label(linha4, text="quadro inteiro e dentro das regiões; 0 a 100", style="Dica.TLabel")
    nota.pack(side="left", padx=(6, 0))
    Dica([campo, nota], DICAS["vmaf"] if tem_vmaf else
         DICAS["vmaf"] + "\n\nO ffmpeg deste computador não tem o filtro libvmaf (nem há um ffvmaf ao lado "
         "do programa), então a medição não está disponível.")
    campo = ttk.Radiobutton(linha4, text="Desenhar as regiões no vídeo (confere a detecção; sem VMAF)",
                            variable=v_verificacao, value="regioes")
    campo.pack(side="left", padx=(22, 0))
    Dica(campo, DICAS["regioes"])

    linha5 = ttk.Frame(topo)
    linha5.grid(row=9, column=0, columnspan=3, sticky="w", pady=(8, 0))
    ttk.Label(linha5, text="Vídeos simultâneos:", style="Rotulo.TLabel").pack(side="left")
    ttk.Spinbox(linha5, from_=1, to=hw.maximo, width=4, textvariable=v_jobs,
                command=lambda: atualizar_dica_jobs()).pack(side="left", padx=(6, 6))
    ttk.Label(linha5, textvariable=v_jobs_dica, style="Dica.TLabel").pack(side="left")
    ttk.Label(topo, text=hw.descricao(), style="Dica.TLabel").grid(row=10, column=0, columnspan=3,
                                                                   sticky="w", pady=(2, 0))

    def confirmar_apagar():
        """Ao marcar, pede confirmação (foco em "Não"); sem confirmação, volta a desmarcar."""
        if v_apagar.get() and not messagebox.askyesno(
                "Apagar arquivos originais", "Deseja realmente apagar os vídeos originais?",
                icon="warning", default="no", parent=janela):
            v_apagar.set(False)

    linha_apagar = ttk.Frame(topo)
    linha_apagar.grid(row=11, column=0, columnspan=3, sticky="w", pady=(8, 0))
    c_apagar = ttk.Checkbutton(linha_apagar, text="Apagar arquivos originais", variable=v_apagar,
                               command=confirmar_apagar, state="disabled")
    c_apagar.pack(side="left")
    v_nota_apagar = tk.StringVar(value=NOTA_APAGAR_BLOQUEADO)
    l_apagar = ttk.Label(linha_apagar, textvariable=v_nota_apagar, style="Dica.TLabel")
    l_apagar.pack(side="left", padx=(6, 0))
    Dica([c_apagar, l_apagar], DICAS["apagar"])

    def atualizar_apagar():
        """A opção só fica disponível com vídeos escolhidos na lista da pasta atual. Sem escolha,
        volta a ficar desmarcada: cada lote pede a confirmação de novo."""
        if estado["rodando"]:
            c_apagar.configure(state="disabled")
            return
        esc = estado.get("escolha")
        pode = bool(esc and esc.get("chave") == chave_da_pasta() and any(it[2] for it in esc["itens"]))
        if not pode and v_apagar.get():
            v_apagar.set(False)
        c_apagar.configure(state="normal" if pode else "disabled")
        nota = NOTA_APAGAR if pode else NOTA_APAGAR_BLOQUEADO
        if v_nota_apagar.get() != nota:
            v_nota_apagar.set(nota)

    def ler_jobs() -> int | None:
        try:
            return int(v_jobs.get())
        except (tk.TclError, ValueError):
            return None

    def atualizar_dica_jobs(*_):
        jobs = ler_jobs()
        if jobs is None or jobs < 1:
            v_jobs_dica.set(f"de 1 a {hw.maximo}  (sugerido {hw.sugerido})")
            return
        extra = f"   acima do máximo, será usado {hw.maximo}" if jobs > hw.maximo else ""
        jobs = hw.limitar(jobs)
        v_jobs_dica.set(f"~{hw.threads_por_conversao(jobs)} núcleo(s) físico(s) por vídeo   "
                        f"(sugerido {hw.sugerido}, máximo {hw.maximo}){extra}")

    v_jobs.trace_add("write", atualizar_dica_jobs)
    atualizar_dica_jobs()

    # Rodapé antes da tabela, para a tabela ocupar o espaço que sobrar.
    base = ttk.Frame(janela, padding=(14, 8, 14, 12))
    base.pack(side="bottom", fill="x")
    base.columnconfigure(1, weight=1)

    meio = ttk.Frame(janela, padding=(14, 10, 14, 0))
    meio.pack(fill="both", expand=True)
    colunas = ("sel", "arquivo", "status", "original", "reduzido", "reducao", "atividade", "eventos",
               "vmaf", "vmaf_roi")
    tabela = ttk.Treeview(meio, columns=colunas, show="headings", selectmode="none")
    for c, titulo, larg, anc in (("sel", MARCADO, 36, "center"),
                                 ("arquivo", "Arquivo", 270, "w"), ("status", "Situação", 200, "w"),
                                 ("original", "Original", 90, "e"), ("reduzido", "Reduzido", 90, "e"),
                                 ("reducao", "Redução", 100, "e"), ("atividade", "Atividade", 140, "e"),
                                 ("eventos", "Eventos", 70, "e"), ("vmaf", "VMAF", 70, "e"),
                                 ("vmaf_roi", "VMAF ROI", 85, "e")):
        tabela.heading(c, text=titulo, anchor=anc)
        tabela.column(c, width=larg, anchor=anc, stretch=(c == "arquivo"))
    tabela.tag_configure("total", background=AZUL_CLARO, foreground=AZUL_ESCURO, font=negrito)
    barra_rol = ttk.Scrollbar(meio, orient="vertical", command=tabela.yview)
    tabela.configure(yscrollcommand=barra_rol.set)
    tabela.pack(side="left", fill="both", expand=True)
    barra_rol.pack(side="right", fill="y")

    ttk.Label(base, text="Em andamento:").grid(row=0, column=0, sticky="w")
    p_arquivo = ttk.Progressbar(base, maximum=1.0)
    p_arquivo.grid(row=0, column=1, sticky="ew", padx=8, pady=3)
    ttk.Label(base, text="Total:").grid(row=1, column=0, sticky="w")
    p_total = ttk.Progressbar(base, maximum=1.0)
    p_total.grid(row=1, column=1, sticky="ew", padx=8, pady=3)
    ttk.Label(base, textvariable=v_status, wraplength=880, justify="left").grid(
        row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
    botoes = ttk.Frame(base)
    botoes.grid(row=0, column=2, rowspan=3, sticky="e", padx=(10, 0))
    b_iniciar = ttk.Button(botoes, text="Iniciar", style="Primario.TButton")
    b_iniciar.pack(fill="x")
    b_cancelar = ttk.Button(botoes, text="Cancelar", state="disabled")
    b_cancelar.pack(fill="x", pady=(6, 0))
    ttk.Button(botoes, text="Registros...", command=lambda: abrir_registros(
        janela, [v_pasta.get().strip(), v_saida.get().strip()])).pack(fill="x", pady=(6, 0))

    def media_de(notas):
        notas = [n for n in notas if n is not None]
        return f"{sum(notas) / len(notas):.1f}" if notas else ""

    def atualizar_total():
        """Última linha da lista: totais das colunas e média do percentual de redução."""
        if not tabela.exists("total"):
            return
        feitos = [r for r in estado["resultados"].values() if r.status in ("Convertido", "Sem ganho")]
        media = f"média {sum(r.reducao or 0 for r in feitos) / len(feitos):.0f}%" if feitos else ""
        tabela.item("total", values=(
            "", f"TOTAL  ({len(estado['pares'])} arquivo(s))",
            f"{len(feitos)} convertido(s) de {len(estado['pares'])}", tamanho(sum(estado["tamanhos"])),
            tamanho(sum(r.tam_destino for r in feitos)) if feitos else "", media,
            tempo(sum(r.ativo for r in feitos)) + " ativos" if feitos else "",
            sum(r.eventos for r in feitos) if feitos else "",
            media_de([r.vmaf for r in feitos]), media_de([r.vmaf_roi for r in feitos])))

    def atualizar_eta():
        if not estado["rodando"] or estado["t0"] is None:
            return
        decorrido = time.monotonic() - estado["t0"]
        feito = estado["dur_feita"] + sum(estado["durs"][i] * p for i, p in estado["andamento"].items()
                                          if i < len(estado["durs"]))
        if estado["dur_total"] <= 0:
            v_eta.set("nada a converter")
        elif feito <= 0 or decorrido < 3:
            v_eta.set(f"calculando...  ({tempo(estado['dur_total'])} de vídeo para processar)")
        else:
            total = decorrido * estado["dur_total"] / feito
            v_eta.set(f"total ~{tempo(total)}   |   restante ~{tempo(total - decorrido)}   |   "
                      f"decorrido {tempo(decorrido)}")

    def trabalhador(raiz_log, pares, aj, jobs):
        resultados: list[Resultado] = []
        try:
            # 1ª passada (vários arquivos ao mesmo tempo): quanto vídeo há para processar.
            eventos.put(("duracoes", duracoes_a_processar(pares, cancelar)))
            avisos = []
            try:
                detectores = PoolDetectores(aj, avisos.append, jobs)
            except Exception as e:               # sem detector não há o que fazer
                eventos.put(("falha", str(e)))
                return
            eventos.put(("detector", detectores.descricao, avisos))
            ultimo: dict[int, int] = {}

            def progresso(i, p):
                if int(p * 500) != ultimo.get(i):    # a análise avisa a cada amostra: não inunda a fila
                    ultimo[i] = int(p * 500)
                    eventos.put(("progresso", i, p))

            resultados = processar_em_lote(
                pares, raiz_log, aj, jobs, detectores, cancelar,
                ao_inicio=lambda i: eventos.put(("inicio", i)), ao_progredir=progresso,
                ao_etapa=lambda i, txt: eventos.put(("etapa", i, txt + "...")),
                ao_fim=lambda i, r: eventos.put(("fim", i, r)))
        finally:
            eventos.put(("acabou", resultados))

    def ler_ajustes():
        """Monta e confere os ajustes da tela. Devolve (pasta de saída, Ajustes, vídeos simultâneos) ou None."""
        saida = None
        if v_onde.get() == "pasta":
            if not v_saida.get().strip():
                messagebox.showwarning("Pasta de saída", "Informe a pasta de saída.", parent=janela)
                return None
            saida = Path(v_saida.get().strip())
        try:
            aj = Ajustes(modo=dict(MODOS_TELA)[v_modo.get()], codec=dict(CODECS_TELA)[v_codec.get()],
                         detector=dict(DETECTORES_TELA)[v_detector.get()],
                         qp_pessoa=int(v_qp["pessoa"].get()), qp_veiculo=int(v_qp["veiculo"].get()),
                         qp_movimento=int(v_qp["movimento"].get()), qp_fundo=int(v_qp["fundo"].get()),
                         fps_ativo=float(v_fps_ativo.get().replace(",", ".")),
                         fps_ocioso=float(v_fps_ocioso.get().replace(",", ".")),
                         mostrar_roi=v_verificacao.get() == "regioes",
                         vmaf=v_verificacao.get() == "vmaf" and tem_vmaf,
                         apagar_original=bool(v_apagar.get()))
        except (tk.TclError, ValueError):
            messagebox.showwarning("Ajustes", "Qualidade e quadros por segundo precisam ser números.",
                                   parent=janela)
            return None
        try:
            aj.validar()
            verificar_ferramentas(aj)
        except Erro as e:
            messagebox.showerror("Ajustes", str(e), parent=janela)
            return None
        jobs = ler_jobs()
        if jobs is None or jobs < 1:
            messagebox.showwarning("Ajustes", f"Vídeos simultâneos: informe um número de 1 a {hw.maximo}.",
                                   parent=janela)
            return None
        if jobs > hw.maximo:
            v_jobs.set(hw.maximo)
            jobs = hw.maximo
        if aj.apagar_original and aj.mostrar_roi:
            messagebox.showinfo("Apagar arquivos originais",
                                "Com \"Desenhar as regiões no vídeo\" o resultado é só um teste: "
                                "os originais NÃO serão apagados.", parent=janela)
        elif aj.apagar_original and aj.modo != "retencao":
            nome = "Time-lapse" if aj.modo == "timelapse" else "Eventos"
            if not messagebox.askyesno(
                    "Apagar arquivos originais",
                    f"No modo {nome}, o vídeo reduzido não guarda tudo o que o original tem "
                    + ("(os trechos sem atividade são acelerados e o áudio é removido)."
                       if aj.modo == "timelapse" else
                       "(só os trechos com atividade são gravados, sem áudio).")
                    + "\n\nApagar mesmo assim os originais depois de convertidos?",
                    icon="warning", default="no", parent=janela):
                return None
        return saida, aj, jobs

    def disparar(pares, raiz, raiz_log, aj, jobs):
        """Preenche a tabela e inicia o processamento."""
        tabela.delete(*tabela.get_children())
        tamanhos = []
        for i, (origem, _) in enumerate(pares):
            try:
                nome = str(origem.relative_to(raiz.resolve()))
            except ValueError:
                nome = origem.name
            tamanhos.append(origem.stat().st_size)
            tabela.insert("", "end", iid=str(i), values=("", nome, "Na fila", tamanho(tamanhos[-1]), "", "", "", "", "", ""))
        tabela.insert("", "end", iid="total", tags=("total",))
        cancelar.clear()
        estado["escolha"] = None                 # a tabela passa a mostrar o andamento
        tabela.heading("sel", text="")
        jobs = min(hw.limitar(jobs), len(pares))
        estado.update(rodando=True, total=len(pares), t0=None, durs=[0.0] * len(pares), dur_total=0.0,
                      dur_feita=0.0, andamento={}, etapas={}, n_fim=0, jobs=jobs, resultados={},
                      pares=pares, tamanhos=tamanhos)
        atualizar_total()
        p_arquivo["value"] = p_total["value"] = 0
        b_iniciar.configure(state="disabled")
        b_cancelar.configure(state="normal")
        v_status.set("Carregando o detector...")
        v_eta.set("analisando os arquivos...")
        threading.Thread(target=trabalhador, args=(raiz_log, pares, aj, jobs), daemon=True).start()

    # ---- lista da pasta, com caixinha para escolher o que processar ----
    def chave_da_pasta():
        return (v_pasta.get().strip(), bool(v_sub.get()))

    def saida_atual():
        return Path(v_saida.get().strip()) if v_onde.get() == "pasta" and v_saida.get().strip() else None

    def atualizar_escolha():
        esc = estado["escolha"]
        if not esc or not tabela.exists("total"):
            return
        marc = [it for it in esc["itens"] if it[2]]
        tabela.heading("sel", text=MARCADO if marc and len(marc) == len(esc["itens"]) else DESMARCADO)
        tabela.item("total", values=("", f"TOTAL  ({len(esc['itens'])} arquivo(s))",
                                     f"{len(marc)} selecionado(s) para processar",
                                     tamanho(sum(it[1] for it in marc)), "", "", "", "", "", ""))
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
                messagebox.showwarning("Pasta", "Escolha uma pasta válida.", parent=janela)
            return False
        v_status.set("Carregando a lista de vídeos...")
        janela.update_idletasks()
        tabela.delete(*tabela.get_children())
        p_arquivo["value"] = p_total["value"] = 0
        v_eta.set("será calculado ao iniciar o processamento")
        itens = []
        for origem, destino in planejar(raiz.resolve(), chave[1], saida_atual())[1]:
            try:
                tam = origem.stat().st_size
            except OSError:
                continue
            feito = destino.exists()
            try:
                nome = str(origem.relative_to(raiz.resolve()))
            except ValueError:
                nome = origem.name
            tabela.insert("", "end", iid=f"e{len(itens)}",
                          values=(DESMARCADO if feito else MARCADO, nome,
                                  "Já reduzido" if feito else "", tamanho(tam), "", "", "", "", "", ""))
            itens.append([origem, tam, not feito])
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
            messagebox.showwarning("Pasta", "Escolha uma pasta válida.", parent=janela)
            return
        esc = estado["escolha"]
        if not esc or esc["chave"] != chave_da_pasta():
            # Lista ainda não carregada (ou mostrando o resultado anterior): carrega para o usuário escolher.
            if carregar_lista() and estado["escolha"]["itens"]:
                v_status.set("Lista carregada. Confira os vídeos marcados e clique em Iniciar de novo.")
            return
        marcados = {it[0] for it in esc["itens"] if it[2]}
        if not marcados:
            messagebox.showwarning("Iniciar", "Nenhum vídeo está marcado na lista.", parent=janela)
            return
        lidos = ler_ajustes()
        if not lidos:
            return
        saida, aj, jobs = lidos
        pares = [p for p in planejar(raiz.resolve(), v_sub.get(), saida)[1] if p[0] in marcados]
        if not pares:
            messagebox.showwarning("Iniciar", "Os vídeos marcados não estão mais disponíveis na pasta.",
                                   parent=janela)
            return
        disparar(pares, raiz, saida or raiz, aj, jobs)

    def pedir_cancelamento():
        cancelar.set()
        b_cancelar.configure(state="disabled")
        v_status.set("Cancelando...")

    def encerrar(texto):
        estado["rodando"] = False
        b_iniciar.configure(state="normal")
        b_cancelar.configure(state="disabled")
        v_status.set(texto)

    def atualizar_andamento():
        """Barras e situação com vários vídeos ao mesmo tempo."""
        andamento = estado["andamento"]
        p_arquivo["value"] = sum(andamento.values()) / len(andamento) if andamento else 0
        if estado["total"]:
            p_total["value"] = (estado["n_fim"] + sum(andamento.values())) / estado["total"]
        if estado["rodando"] and not cancelar.is_set():
            v_status.set(f"Concluídos {estado['n_fim']} de {estado['total']}  ·  {len(andamento)} em andamento "
                         f"(até {estado['jobs']} ao mesmo tempo).  Detector: {estado.get('detector', '')}")

    def processar_eventos():
        try:
            while True:
                ev = eventos.get_nowait()
                if ev[0] == "duracoes":
                    estado.update(durs=ev[1], dur_total=sum(ev[1]))
                elif ev[0] == "detector":
                    estado["t0"] = time.monotonic()
                    estado["detector"] = ev[1] + ("  " + " ".join(ev[2]) if ev[2] else "")
                elif ev[0] == "falha":
                    encerrar("Não foi possível iniciar.")
                    v_eta.set("processamento não iniciado")
                    messagebox.showerror("Detector", ev[1], parent=janela)
                elif ev[0] == "inicio":
                    estado["andamento"][ev[1]] = 0.0
                    estado["etapas"][ev[1]] = "Iniciando..."
                    tabela.set(str(ev[1]), "status", "Iniciando...")
                    tabela.see(str(ev[1]))
                    atualizar_andamento()
                elif ev[0] == "etapa":
                    estado["etapas"][ev[1]] = ev[2]
                    tabela.set(str(ev[1]), "status", ev[2])
                elif ev[0] == "progresso":
                    if ev[1] in estado["andamento"]:
                        estado["andamento"][ev[1]] = ev[2]
                        tabela.set(str(ev[1]), "status",
                                   f"{estado['etapas'].get(ev[1], '')}  {ev[2] * 100:.0f}%".strip())
                        atualizar_andamento()
                elif ev[0] == "fim":
                    i, r = ev[1], ev[2]
                    tabela.set(str(i), "status", r.status + (f" - {r.detalhe}" if r.detalhe else ""))
                    if r.tam_destino:
                        tabela.set(str(i), "reduzido", tamanho(r.tam_destino))
                    if r.reducao is not None:
                        tabela.set(str(i), "reducao", f"{r.reducao:.0f}%")
                    if r.status in ("Convertido", "Sem ganho"):
                        tabela.set(str(i), "atividade", f"{tempo(r.ativo)} de {tempo(r.duracao)}")
                        tabela.set(str(i), "eventos", r.eventos)
                    if r.vmaf is not None:
                        tabela.set(str(i), "vmaf", f"{r.vmaf:.1f}")
                    if r.vmaf_roi is not None:
                        tabela.set(str(i), "vmaf_roi", f"{r.vmaf_roi:.1f}")
                    estado["andamento"].pop(i, None)
                    estado["etapas"].pop(i, None)
                    estado["n_fim"] += 1
                    estado["dur_feita"] += estado["durs"][i] if i < len(estado["durs"]) else 0.0
                    estado["resultados"][i] = r
                    atualizar_andamento()
                    atualizar_total()
                elif ev[0] == "acabou":
                    decorrido = time.monotonic() - estado["t0"] if estado["t0"] else 0
                    tabela.see("total")
                    prefixo = "Cancelado. " if cancelar.is_set() else "Concluído. "
                    encerrar(prefixo + resumo(ev[1]) + f"  Registro em {NOME_LOG}.  "
                             f"Detector: {estado.get('detector', '')}")
                    v_eta.set(f"processamento {'cancelado após' if cancelar.is_set() else 'concluído em'} "
                              f"{tempo(decorrido)}")
        except queue.Empty:
            pass
        atualizar_eta()
        atualizar_apagar()
        janela.after(200, processar_eventos)

    def fechar():
        if estado["rodando"] and not messagebox.askyesno(
                "Sair", "Há um processamento em andamento. Cancelar e sair?", parent=janela):
            return
        cancelar.set()
        janela.destroy()

    b_iniciar.configure(command=iniciar)
    b_cancelar.configure(command=pedir_cancelamento)
    janela.protocol("WM_DELETE_WINDOW", fechar)
    atualizar_onde()
    processar_eventos()
    janela.mainloop()
    return 0


# --------------------------------------------------------------------------
# Linha de comando
# --------------------------------------------------------------------------
def ler_argumentos(argv=None) -> tuple[argparse.Namespace, Ajustes]:
    p = argparse.ArgumentParser(description=f"{TITULO} {VERSAO}")
    padrao = Ajustes()
    p.add_argument("alvo", nargs="?", help="vídeo ou pasta de vídeos; sem ele, abre a janela")
    p.add_argument("--saida", help="pasta de saída (padrão: ao lado do original)")
    p.add_argument("--subpastas", action="store_true", help="inclui as subpastas")
    p.add_argument("--modo", choices=MODOS, default=padrao.modo)
    p.add_argument("--codec", choices=sorted(CODIFICADORES), default=padrao.codec,
                   help="hevc e h264 aplicam o ROI por QP; *_qsv usam a GPU Intel; av1 não tem ROI")
    p.add_argument("--preset", default="", help="preset do codificador (padrão: medium; av1: 8)")
    p.add_argument("--qp-fundo", type=int, default=padrao.qp_fundo, help="qualidade do fundo (CRF/QP)")
    p.add_argument("--qp-pessoa", type=int, default=padrao.qp_pessoa)
    p.add_argument("--qp-veiculo", type=int, default=padrao.qp_veiculo)
    p.add_argument("--qp-movimento", type=int, default=padrao.qp_movimento)
    p.add_argument("--fps-ativo", type=float, default=padrao.fps_ativo,
                   help="quadros/s com atividade (0 = taxa original)")
    p.add_argument("--fps-ocioso", type=float, default=padrao.fps_ocioso, help="quadros/s sem atividade")
    p.add_argument("--fps-analise", type=float, default=padrao.fps_analise)
    p.add_argument("--lado-analise", type=int, default=padrao.lado_analise,
                   help="maior lado do quadro analisado; aumente para objetos distantes")
    p.add_argument("--janela", type=float, default=padrao.janela, help="segundos de cada trecho ativo")
    p.add_argument("--antes", type=float, default=padrao.antes, help="segundos mantidos antes da atividade")
    p.add_argument("--depois", type=float, default=padrao.depois, help="segundos mantidos depois")
    p.add_argument("--sensibilidade", type=float, default=padrao.sensibilidade,
                   help="fração mínima do quadro que conta como movimento")
    p.add_argument("--detector", choices=("auto", "openvino", "yolo", "movimento"), default=padrao.detector)
    p.add_argument("--modelo", default=padrao.modelo, help="modelo YOLO (.pt, .onnx ou pasta OpenVINO)")
    p.add_argument("--confianca", type=float, default=padrao.confianca)
    p.add_argument("--dispositivo", default="", help="onde rodar o YOLO, repassado à ultralytics (ex.: cpu, 0, xpu, intel:gpu)")
    p.add_argument("--sem-audio", action="store_true")
    p.add_argument("--jobs", type=int, default=None,
                   help="vídeos processados ao mesmo tempo (padrão: o sugerido para este computador; "
                        "acima do máximo, usa o máximo)")
    p.add_argument("--hardware", action="store_true",
                   help="mostra o processador detectado e os limites de vídeos simultâneos, e sai")
    p.add_argument("--paralelo", type=int, default=0, help="trechos de um vídeo codificados ao mesmo tempo")
    p.add_argument("--mostrar-roi", action="store_true", help="desenha as regiões no vídeo (diagnóstico)")
    p.add_argument("--sem-hash", action="store_true", help="não calcula o SHA-256 dos arquivos")
    p.add_argument("--apagar-originais", action="store_true",
                   help="apaga cada original depois de convertido com sucesso (sem confirmação, não vai "
                        "para a Lixeira; nunca com --mostrar-roi)")
    p.add_argument("--sem-vmaf", action="store_true", help="não mede o índice VMAF (mais rápido)")
    p.add_argument("--versao", action="version", version=f"{TITULO} {VERSAO}")
    p.add_argument("--registros", nargs="?", const="", metavar="PASTA_OU_CSV",
                   help="mostra os vídeos já processados (sem valor: todos os registros conhecidos)")
    p.add_argument("--situacao", default="", help="com --registros: só esta situação (ex.: Erro)")
    a = p.parse_args(argv)
    aj = Ajustes(modo=a.modo, codec=a.codec, preset=a.preset, qp_fundo=a.qp_fundo, qp_pessoa=a.qp_pessoa,
                 qp_veiculo=a.qp_veiculo, qp_movimento=a.qp_movimento, fps_ativo=a.fps_ativo,
                 fps_ocioso=a.fps_ocioso, fps_analise=a.fps_analise, lado_analise=a.lado_analise,
                 janela=a.janela, antes=a.antes, depois=a.depois, sensibilidade=a.sensibilidade,
                 detector=a.detector, modelo=a.modelo, confianca=a.confianca, dispositivo=a.dispositivo,
                 audio=not a.sem_audio, paralelo=a.paralelo, mostrar_roi=a.mostrar_roi,
                 calcular_hash=not a.sem_hash, vmaf=not a.sem_vmaf, apagar_original=a.apagar_originais)
    return a, aj


def main(argv=None) -> int:
    args, aj = ler_argumentos(argv)
    if args.hardware:
        hw = hardware()
        print(hw.descricao())
        print(f"Vídeos simultâneos: sugerido {hw.sugerido}, máximo {hw.maximo} "
              f"(~{hw.threads_por_conversao(hw.sugerido)} núcleo(s) físico(s) por vídeo no sugerido).")
        return 0
    if args.registros is not None:
        return imprimir_registros(args.registros, args.situacao)
    if args.alvo is None:
        return rodar_gui()
    alvo = Path(args.alvo).expanduser().resolve()
    pasta_saida = Path(args.saida).expanduser().resolve() if args.saida else None
    try:
        aj.validar()
        if not alvo.exists():
            raise Erro(f"não encontrado: {alvo}")
        verificar_ferramentas(aj)
        raiz, pares = planejar(alvo, args.subpastas, pasta_saida)
        if not pares:
            raise Erro("nenhum vídeo para processar.")
        hw = hardware()
        jobs = hw.sugerido if args.jobs is None else args.jobs
        if jobs > hw.maximo:
            print(f"Aviso: --jobs {jobs} passa do máximo deste computador; usando {hw.maximo}.")
        jobs = min(hw.limitar(jobs), len(pares))
        detectores = PoolDetectores(aj, print, jobs)
    except Erro as e:
        print(f"Erro: {e}", file=sys.stderr)
        return 2
    print(f"{TITULO} {VERSAO}")
    print(f"Modo {aj.modo}, codec {CODIFICADORES[aj.codec][0]}, detector: {detectores.descricao}")
    print(f"{hw.descricao()}\n{jobs} vídeo(s) ao mesmo tempo (sugerido {hw.sugerido}, máximo {hw.maximo}), "
          f"~{hw.threads_por_conversao(jobs)} núcleo(s) físico(s) cada.")
    if not CODIFICADORES[aj.codec][1]:
        print("Aviso: este codec não aplica o ROI por QP; só a redução temporal será usada.")
    if aj.vmaf and ferramenta_vmaf() is None:
        print("Aviso: este ffmpeg não tem o filtro libvmaf; a qualidade não será medida.")
    elif aj.vmaf and aj.mostrar_roi:
        print("Aviso: com as regiões desenhadas no vídeo, o VMAF não é medido.")
    cancelar = threading.Event()
    trava = threading.Lock()
    andamento: dict[int, tuple[str, float]] = {}
    contagem = [0]
    largura = max(40, min(shutil.get_terminal_size((100, 20)).columns - 1, 170))

    def linha_andamento():
        partes = [f"{pares[i][0].name[:24]} {etapa} {p * 100:3.0f}%" for i, (etapa, p) in sorted(andamento.items())]
        texto = f"[{contagem[0]}/{len(pares)}] " + "  |  ".join(partes)
        print("\r" + texto[:largura].ljust(largura), end="", flush=True)

    def ao_inicio(i):
        with trava:
            andamento[i] = ("", 0.0)
            linha_andamento()

    def ao_etapa(i, texto):
        with trava:
            if i in andamento:
                andamento[i] = (texto, andamento[i][1])
                linha_andamento()

    def ao_progredir(i, p):
        with trava:
            if i in andamento and int(p * 100) != int(andamento[i][1] * 100):
                andamento[i] = (andamento[i][0], p)
                linha_andamento()

    def ao_fim(i, r):
        with trava:
            andamento.pop(i, None)
            contagem[0] += 1
            linha = f"[{contagem[0]}/{len(pares)}] {r.origem.name[:48]}: {r.status}"
            if r.reducao is not None and r.status in ("Convertido", "Sem ganho"):
                linha += (f" - {tamanho(r.tam_origem)} -> {tamanho(r.tam_destino)} ({r.reducao:.1f}% menor), "
                          f"{r.eventos} evento(s), {tempo(r.ativo)} ativos de {tempo(r.duracao)}, "
                          f"em {tempo(r.segundos)}")
                if r.vmaf is not None:
                    linha += f", VMAF {r.vmaf:.1f}" + (f" (regiões {r.vmaf_roi:.1f})" if r.vmaf_roi is not None else "")
                if r.detalhe:
                    linha += f"; {r.detalhe}"
            elif r.detalhe:
                linha += f" - {r.detalhe}"
            print("\r" + linha.ljust(largura))
            if andamento:
                linha_andamento()

    try:
        resultados = processar_em_lote(pares, pasta_saida or raiz, aj, jobs, detectores, cancelar,
                                       ao_inicio, ao_progredir, ao_etapa, ao_fim)
    except KeyboardInterrupt:
        print("\nCancelado.")
        return 130
    feitos = [r for r in resultados if r.status == "Convertido"]
    if feitos:
        antes, depois = sum(r.tam_origem for r in feitos), sum(r.tam_destino for r in feitos)
        print(f"Total: {len(feitos)} vídeo(s), {tamanho(antes)} -> {tamanho(depois)} "
              f"({100 * (1 - depois / antes):.1f}% menor)")
    return 1 if any(r.status == "Erro" for r in resultados) else 0


if __name__ == "__main__":
    sys.exit(main())
