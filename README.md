# Motor de Otimização e Retenção

Compactador de Vídeos (C&C): varre uma pasta e reduz os vídeos convertendo para AV1 (SVT-AV1) com o ffmpeg, mantendo o original e o reduzido lado a lado (sufixo ` - RED`). Também procura vídeos grandes no computador.

## Requisitos

- Python 3.9 ou superior (com Tkinter para a janela)
- `ffmpeg` e `ffprobe` no PATH, com o codificador `libsvtav1`

O script usa apenas a biblioteca padrão do Python.

## Uso

```
python conversor_av1.py                    # abre a janela
python conversor_av1.py --cli "C:\Videos"  # roda no terminal (bom para agendar)
python conversor_av1.py --help             # lista as opções
```

O resultado de cada rodada é registrado em `compactacao_log.csv`.
