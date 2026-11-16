# Motor de Otimização e Retenção com ROI

Reduz vídeos de segurança combinando **desconstrução temporal** (menos quadros quando nada acontece) e **compressão seletiva por região de interesse** (mais qualidade em pessoas, veículos e movimento; fundo fortemente comprimido). A saída é um MP4 comum, que abre em qualquer player.

Projeto distinto do Compactador de Vídeos AV1 (`Motor-de-Otimizacao`), que converte o vídeo inteiro com uma única qualidade.

## Como funciona

```
Vídeo ──► Análise (ffmpeg + OpenCV + YOLOv8) ──► Linha do tempo ──► Codificação por trechos ──► MP4 + JSON
          5 quadros/s em baixa resolução          ativo / ocioso     FPS e mapa de QP por trecho
```

1. **Análise.** O ffmpeg entrega quadros reduzidos; o OpenCV detecta movimento (subtração de fundo) e o YOLOv8 detecta pessoas e veículos. Veículo parado é tratado como cenário.
2. **Linha do tempo.** Cada instante é marcado como ativo (há movimento ou pessoa) ou ocioso, com folga antes e depois de cada atividade.
3. **Codificação.** Cada trecho é codificado com a taxa de quadros do seu estado e com as regiões de interesse aplicadas pelo filtro `addroi` do ffmpeg (deslocamento de QP por região). Os trechos são unidos sem recodificar; o áudio e os metadados vêm do original.

| Região | QP padrão | Opção |
|---|---|---|
| Pessoa | 22 | `--qp-pessoa` |
| Veículo em movimento | 26 | `--qp-veiculo` |
| Outro movimento | 30 | `--qp-movimento` |
| Fundo | 36 | `--qp-fundo` |

| Situação | Quadros/s padrão | Opção |
|---|---|---|
| Com atividade | 15 | `--fps-ativo` (0 mantém a taxa original) |
| Sem atividade | 1 | `--fps-ocioso` |

## Modos

| `--modo` | O que grava | Duração da saída | Áudio |
|---|---|---|---|
| `retencao` (padrão) | Tudo; trechos ociosos em baixa taxa de quadros | Igual à do original | Mantido |
| `timelapse` | Tudo; trechos ociosos acelerados | Menor | Removido |
| `eventos` | Só os trechos com atividade | Soma dos eventos | Removido |

## Requisitos

- Python 3.9 ou superior
- `ffmpeg` e `ffprobe` no PATH, com `libx265`
- `pip install -r requirements.txt` (numpy e opencv-python)
- Tkinter para a janela (já vem com o Python do Windows)
- Opcional: `pip install ultralytics` para o YOLOv8 e `pip install openvino` para rodá-lo em CPU, GPU ou NPU Intel

Sem a `ultralytics`, o programa funciona usando apenas o movimento como região de interesse.

O valor de `--dispositivo` é repassado à ultralytics. Para a GPU Intel com OpenVINO, as versões recentes usam `xpu` e as anteriores `intel:gpu`.

## Uso

```
python motor_roi.py                                   # abre a janela
python motor_roi.py "C:\Videos"                       # todos os vídeos da pasta, no terminal
python motor_roi.py "C:\Videos" --subpastas --saida "D:\Reduzidos"
python motor_roi.py camera1.mp4 --modo eventos        # só os eventos
python motor_roi.py camera1.mp4 --mostrar-roi         # desenha as regiões (para conferir a detecção)
python motor_roi.py camera1.mp4 --detector openvino --dispositivo xpu   # GPU Intel
python motor_roi.py --help
python motor_roi.py --hardware                        # processador detectado e limites
python motor_roi.py "C:\Videos" --jobs 3              # 3 vídeos ao mesmo tempo
```

Sem argumentos, o programa abre a janela: escolha a pasta, marque os vídeos, ajuste modo, codec, detector, qualidade por região e quadros por segundo, e clique em Iniciar. As opções menos usadas (`--janela`, `--antes`, `--depois`, `--sensibilidade`, `--lado-analise`, `--modelo`, `--dispositivo`) ficam só na linha de comando.

Para cada vídeo são gravados, ao lado do original:

- `nome - ROI.mp4`: o vídeo reduzido;
- `nome - ROI.json`: o manifesto, com os ajustes usados, os eventos (início, fim, pessoas, veículos), os trechos com suas regiões e a posição de cada trecho na saída, além do SHA-256 do original e do reduzido;
- `motor_roi_log.csv`: uma linha por vídeo processado, com as notas VMAF.

O original não é alterado. Ele só é apagado se a opção **Apagar arquivos originais** estiver marcada (ou com `--apagar-originais` no terminal), e somente depois que o reduzido foi gravado, passou na conferência de duração, ficou menor que o original e teve o manifesto `.json` gravado. O arquivo é apagado sem passar pela Lixeira; o manifesto registra `"apagado": true` na parte da origem. Vídeos com erro, cancelados, ignorados ou sem ganho mantêm o original, e com `--mostrar-roi` (regiões desenhadas) nada é apagado.

## Vídeos simultâneos

O programa detecta o processador (núcleos físicos) e a memória e processa vários vídeos ao mesmo
tempo. Cada vídeo fica preso ao seu grupo de núcleos (análise, codificação dos trechos e VMAF) e
usa o seu próprio detector YOLO, porque o mesmo modelo não pode atender duas análises ao mesmo tempo.

- **Sugerido:** cerca de 4 núcleos físicos por vídeo.
- **Máximo:** pelo menos 2 núcleos físicos e 3 GB de RAM por vídeo.
- Na janela, o campo "Vídeos simultâneos" aceita de 1 até o máximo; no terminal, `--jobs N`
  (sem a opção, usa o sugerido; acima do máximo, usa o máximo).
- Os maiores arquivos começam primeiro.
- `--paralelo` continua valendo para os trechos de cada vídeo; sem ele, o número de trechos
  simultâneos é calculado pelos núcleos reservados ao vídeo.
- Com 1 vídeo por vez, o comportamento é o mesmo das versões anteriores.

## Consulta dos registros

O botão **Registros...** abre a lista dos vídeos já processados, com as mesmas colunas da tela de processamento (situação, tamanhos, redução, atividade, eventos, VMAF e VMAF ROI) e a data e hora de cada processamento. Dá para filtrar por nome, situação e período, ordenar clicando no título da coluna, exportar o resultado em CSV ou abri-lo no Excel. Duplo clique numa linha mostra o vídeo na pasta.

Cada `motor_roi_log.csv` gravado é anotado em `%APPDATA%\CC\motor-roi\registros_conhecidos.txt`, e assim a consulta reúne os registros de todas as pastas. Registros gravados antes desta versão entram pelo botão **Procurar em pasta...**.

```
python motor_roi.py --registros                          # todos os registros conhecidos
python motor_roi.py --registros "C:\Videos" --situacao Erro
```

## Medição de qualidade (VMAF)

Se o ffmpeg tiver o filtro `libvmaf` (ou houver um `ffvmaf` ao lado do programa), cada vídeo convertido é comparado com o original e recebe duas notas de 0 a 100:

- **VMAF**: quadro inteiro, média de todos os quadros mantidos;
- **VMAF ROI**: só dentro da região de interesse principal de cada trecho.

A comparação é feita trecho a trecho, quadro mantido contra o mesmo quadro do original. Os quadros descartados pela redução temporal não entram na conta. O manifesto `.json` traz também as médias dos trechos ativos e ociosos, a pior nota e a nota de cada trecho. Use `--sem-vmaf` para pular a medição; com `--mostrar-roi` ela não é feita, porque as caixas desenhadas alterariam a nota.

## Instalador para Windows

`Instalar_Motor_ROI_CC_<versão>.exe` instala o programa para o usuário atual, sem exigir administrador, com atalhos no Menu Iniciar e na Área de Trabalho. Ele já traz Python 3.12, Tkinter, ffmpeg (com libx265, libx264 e SVT-AV1), `ffvmaf` para a medição VMAF, NumPy, OpenCV, PyTorch (CPU), ultralytics e o modelo `yolov8n.pt`. Não inclui o OpenVINO.

### Como o instalador é gerado

O instalador é montado do zero por `instalador\montar_instalador.ps1`. O script baixa o Python e o ffmpeg do conda-forge (pelo micromamba), o `ffvmaf` do pacote imageio-ffmpeg, as bibliotecas do PyPI e o modelo YOLOv8. Depois confere se o programa abre e empacota tudo com o NSIS (`instalador\instalador_roi.nsi`) em `dist\Instalar_Motor_ROI_CC_<versão>.exe`.

**Geração automática:** a versão fica na constante `VERSAO` de `motor_roi.py`. A cada `push` na `main` com uma versão que ainda não tem release, o GitHub Actions (`.github\workflows\instalador.yml`) monta o instalador num Windows e o publica em **Releases** com a tag `v<versão>`. Para lançar uma versão nova, basta mudar `VERSAO` e enviar. Para gerar de novo a mesma versão, use **Actions > Instalador > Run workflow**.

**No próprio PC** (precisa de internet e do [NSIS](https://nsis.sourceforge.io)):

```
powershell -ExecutionPolicy Bypass -File instalador\montar_instalador.ps1
```

As pastas `build\` e `dist\` não vão para o repositório.

Para empacotar mais rápido (o instalador fica maior), use `makensis -DRAPIDO`: troca a compressão LZMA pela zlib.

A ultralytics e o modelo YOLOv8 são licenciados sob a AGPL-3.0; avalie essa licença antes de distribuir o instalador fora da empresa.

## Codificadores

| `--codec` | Codificador | ROI por QP | Observação |
|---|---|---|---|
| `hevc` (padrão) | libx265 | Sim | Testado |
| `h264` | libx264 | Sim | Testado; arquivos maiores, compatibilidade máxima |
| `hevc_qsv`, `h264_qsv` | Intel Quick Sync | Sim, pelo driver | Usa a GPU Intel; ainda não validado em hardware |
| `av1` | libsvtav1 | Não | Só a redução temporal: o SVT-AV1 ignora as regiões do `addroi` |

## Limites conhecidos

- A região vale para o trecho inteiro (`--janela`, 4 s por padrão): ela cobre todo o caminho do objeto dentro do trecho, não quadro a quadro.
- Câmera em movimento (PTZ, filmagem na mão) faz o quadro todo contar como movimento: o vídeo fica todo "ativo" e só as detecções do YOLO viram região.
- Objetos pequenos ou distantes podem escapar da análise em 640 px; use `--lado-analise 1280`.
