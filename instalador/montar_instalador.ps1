<#
  Monta o instalador do Motor ROI, do zero:
    - Python + Tkinter e ffmpeg (com x264, x265 e SVT-AV1) vindos do conda-forge, pelo micromamba;
    - ffvmaf (ffmpeg com libvmaf) vindo do pacote imageio-ffmpeg do PyPI;
    - NumPy, OpenCV, PyTorch (CPU), ultralytics e o modelo yolov8n.pt;
    - empacota tudo com o NSIS em dist\Instalar_Motor_ROI_CC_<versão>.exe.
  O instalador do Compactador de Vídeos (AV1) é montado no repositório dele (Motor-de-Otimizacao).

  Uso (PowerShell, na pasta do projeto):
    powershell -ExecutionPolicy Bypass -File instalador\montar_instalador.ps1

  A versão vem da constante VERSAO do programa. Precisa de internet e do NSIS (makensis);
  no GitHub Actions (.github\workflows\instalador.yml) roda sozinho a cada versão nova.
#>
param(
    [ValidateSet("roi")][string]$Tipo = "roi",
    [string]$Versao = "",
    [string]$Python = "3.12"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$raiz = Split-Path -Parent $PSScriptRoot
$cfg = @{ Fonte = "motor_roi.py"; Programa = "motor_roi.pyw"; Nsi = "instalador_roi.nsi"
         Saida = "Instalar_Motor_ROI_CC"; Codecs = @("libx265", "libx264", "libsvtav1") }

$fonte = Join-Path $raiz $cfg.Fonte
if (-not $Versao) {
    $achado = Select-String -Path $fonte -Pattern '^VERSAO\s*=\s*"([^"]+)"' | Select-Object -First 1
    if (-not $achado) { throw "VERSAO não encontrada em $($cfg.Fonte)" }
    $Versao = $achado.Matches[0].Groups[1].Value
}
Write-Host "== $($cfg.Saida) $Versao"

$obra = Join-Path $raiz "build\$Tipo"
$motor = Join-Path $obra "motor"            # vira a pasta "motor" da instalação
$cache = Join-Path $raiz "build\cache"
$dist = Join-Path $raiz "dist"
if (Test-Path $obra) { Remove-Item -Recurse -Force $obra }
New-Item -ItemType Directory -Force $motor, $cache, $dist | Out-Null

function Baixar([string]$url, [string]$destino) {
    if (Test-Path $destino) { return }
    Write-Host "   baixando $url"
    Invoke-WebRequest -Uri $url -OutFile "$destino.tmp" -UseBasicParsing
    Move-Item "$destino.tmp" $destino
}

function Rodar([string]$exe) {
    & $exe @args
    if ($LASTEXITCODE -ne 0) { throw "falhou (código $LASTEXITCODE): $exe $args" }
}

# ---- micromamba (instalador de pacotes do conda-forge, sem precisar do Anaconda) ----
$mm = Join-Path $cache "micromamba\Library\bin\micromamba.exe"
if (-not (Test-Path $mm)) {
    $pacote = Join-Path $cache "micromamba.tar.bz2"
    Baixar "https://micro.mamba.pm/api/micromamba/win-64/latest" $pacote
    New-Item -ItemType Directory -Force (Join-Path $cache "micromamba") | Out-Null
    Rodar "$env:SystemRoot\System32\tar.exe" -xjf $pacote -C (Join-Path $cache "micromamba")
}
$env:MAMBA_ROOT_PREFIX = Join-Path $cache "mamba"

# ---- Python com Tkinter ----
Write-Host "-- Python $Python"
$pyDir = Join-Path $motor "python"
Rodar $mm create -y -q --always-copy -p $pyDir -c conda-forge --override-channels "python=$Python" tk pip
$py = Join-Path $pyDir "python.exe"
$env:PATH = "$pyDir;$pyDir\Library\bin;$pyDir\Scripts;$env:PATH"
$env:PYTHONNOUSERSITE = "1"
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"

if ($Tipo -eq "roi") {
    Write-Host "-- bibliotecas do ROI (PyTorch CPU, OpenCV, ultralytics)"
    Rodar $py -m pip install --no-cache-dir --no-warn-script-location torch torchvision `
        --index-url https://download.pytorch.org/whl/cpu
    Rodar $py -m pip install --no-cache-dir --no-warn-script-location -r (Join-Path $raiz "requirements.txt") ultralytics
}

# ---- ffmpeg (conda-forge, variante GPL) + ffvmaf (imageio-ffmpeg) ----
Write-Host "-- ffmpeg"
$ffEnv = Join-Path $obra "ffmpeg_env"
Rodar $mm create -y -q --always-copy -p $ffEnv -c conda-forge --override-channels "ffmpeg=*=gpl*"
$ff = Join-Path $motor "ffmpeg"
New-Item -ItemType Directory -Force $ff | Out-Null
Copy-Item "$ffEnv\Library\bin\ffmpeg.exe", "$ffEnv\Library\bin\ffprobe.exe", "$ffEnv\Library\bin\*.dll" $ff
Get-ChildItem $ffEnv -Filter *.dll | Copy-Item -Destination $ff -Force

$whl = Join-Path $obra "imageio"
Rodar $py -m pip download -q --no-deps --only-binary=:all: --platform win_amd64 -d $whl imageio-ffmpeg
$zip = Join-Path $whl "imageio.zip"
Copy-Item (Get-ChildItem $whl -Filter *.whl | Select-Object -First 1).FullName $zip
Expand-Archive $zip (Join-Path $whl "x")
$vmaf = Get-ChildItem (Join-Path $whl "x") -Recurse -Filter "ffmpeg-win*.exe" | Select-Object -First 1
if ($vmaf) { Copy-Item $vmaf.FullName (Join-Path $ff "ffvmaf.exe") }

$encoders = (& "$ff\ffmpeg.exe" -hide_banner -encoders) -join "`n"
foreach ($c in $cfg.Codecs) {
    if ($encoders -notmatch " $c ") { throw "o ffmpeg baixado não tem o codificador $c" }
}
$filtros = if ($vmaf) { (& "$ff\ffvmaf.exe" -hide_banner -filters) -join "`n" } else { "" }
if ($filtros -notmatch " libvmaf ") { Write-Warning "sem ffvmaf com libvmaf: a medição VMAF ficará indisponível" }

# ---- programa, modelo, ícone e licenças ----
Copy-Item $fonte (Join-Path $motor $cfg.Programa)
Copy-Item (Join-Path $PSScriptRoot "cc.ico") $motor
Copy-Item (Join-Path $PSScriptRoot "LICENCAS_$Tipo.txt") (Join-Path $motor "LICENCAS.txt")
if ($Tipo -eq "roi") {
    $pt = Join-Path $raiz "yolov8n.pt"
    if (-not (Test-Path $pt)) {
        $pt = Join-Path $cache "yolov8n.pt"
        Baixar "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.pt" $pt
    }
    Copy-Item $pt $motor
}

# ---- conferência: o Python da instalação abre o programa ----
Write-Host "-- conferindo"
$importa = if ($Tipo -eq "roi") { "import tkinter, numpy, cv2, torch, ultralytics" } else { "import tkinter" }
Rodar $py -c $importa
Push-Location $motor
try { Rodar $py (Join-Path $motor $cfg.Programa) --help | Out-Null } finally { Pop-Location }

# ---- limpeza do que não é usado em tempo de execução ----
foreach ($p in "$pyDir\include", "$pyDir\Library\include", "$pyDir\Lib\test", "$pyDir\pkgs",
               "$pyDir\Lib\site-packages\torch\include") {
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}
Get-ChildItem "$pyDir\Lib\site-packages\torch\lib" -Filter *.lib -ErrorAction SilentlyContinue | Remove-Item -Force

# ---- NSIS ----
$makensis = (Get-Command makensis -ErrorAction SilentlyContinue).Source
if (-not $makensis) {
    $makensis = @("${env:ProgramFiles(x86)}\NSIS\makensis.exe", "$env:ProgramFiles\NSIS\makensis.exe") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $makensis -and (Get-Command choco -ErrorAction SilentlyContinue)) {
    choco install nsis -y --no-progress | Out-Null
    $makensis = "${env:ProgramFiles(x86)}\NSIS\makensis.exe"
}
if (-not $makensis -or -not (Test-Path $makensis)) {
    throw "NSIS (makensis) não encontrado. Instale em https://nsis.sourceforge.io e rode de novo."
}
$exe = Join-Path $dist "$($cfg.Saida)_$Versao.exe"
Write-Host "-- empacotando"
Rodar $makensis /V2 "/DVERSAO=$Versao" "/DMOTOR=$motor" "/DSAIDA=$exe" (Join-Path $PSScriptRoot $cfg.Nsi)
Write-Host ("== pronto: {0} ({1:N0} MB)" -f $exe, ((Get-Item $exe).Length / 1MB))
if ($env:GITHUB_OUTPUT) { "instalador=$exe" | Add-Content $env:GITHUB_OUTPUT }
