<#
  Запуск DragAPI на Windows: бэкенд и интерфейс одной командой (аналог start.sh).

    .\start.ps1                 обычный режим: собрать интерфейс (если менялся) и запустить платформу
                                -> http://localhost:8765
    .\start.ps1 dev             разработка: бэкенд в отдельном окне + Vite с мгновенным обновлением
                                -> http://localhost:5173
    .\start.ps1 setup           только поставить / обновить зависимости
    .\start.ps1 [dev] ФЛАГИ     флаги передаются серверу: --no-auth, --port 9000, --host 0.0.0.0,
                                --no-local-agent, --config FILE, --tls-self-signed

  Двойной щелчок по start.cmd — то же, что .\start.ps1 (без флагов).
  Нужны Python 3.10+ (python.org, галочка «Add python.exe to PATH») и Node.js 18+ (nodejs.org).
#>
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$env:PYTHONUTF8 = '1'   # кириллица в выводе Python

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Backend = Join-Path $Root 'backend'
$Frontend = Join-Path $Root 'frontend'
$Venv = Join-Path $Backend '.venv'
$Py = Join-Path $Venv 'Scripts\python.exe'

function Say($t) { Write-Host "> $t" -ForegroundColor Cyan }
function Ok($t) { Write-Host "OK $t" -ForegroundColor Green }
function Warn($t) { Write-Host "! $t" -ForegroundColor Yellow }
function Die($t) { Write-Host "X $t" -ForegroundColor Red; exit 1 }

$Rest = @($args)
$Mode = 'prod'
if ($Rest.Count -gt 0 -and @('dev', 'setup', 'help', '-h', '--help') -contains $Rest[0]) {
  $Mode = $Rest[0]
  $Rest = @($Rest | Select-Object -Skip 1)
}
if (@('help', '-h', '--help') -contains $Mode) {
  Get-Content $MyInvocation.MyCommand.Path -TotalCount 15 | Select-Object -Skip 1
  exit 0
}

# порт и схема из флагов
$Port = 8765
$Scheme = 'http'
for ($i = 0; $i -lt $Rest.Count; $i++) {
  $a = [string]$Rest[$i]
  if ($a -eq '--port' -and $i + 1 -lt $Rest.Count) { $Port = [int]$Rest[$i + 1] }
  elseif ($a -match '^--port=(\d+)$') { $Port = [int]$Matches[1] }
  elseif ($a -match '^--tls-(self-signed|cert)') { $Scheme = 'https' }
}
$FrontPort = if ($env:DRAGAPI_FRONT_PORT) { [int]$env:DRAGAPI_FRONT_PORT } else { 5173 }

# ---------- проверки ----------
function Find-Python {
  # py -3 (лаунчер python.org), затем python / python3; заглушка из Microsoft Store не подходит
  foreach ($cand in @(@('py', '-3'), @('python'), @('python3'))) {
    $exe = $cand[0]
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    $extra = @($cand | Select-Object -Skip 1)
    try {
      $ok = & $exe @extra -c "import sys; print(sys.version_info >= (3, 10))" 2>$null
      if ($ok -eq 'True') { return , $cand }
    } catch { }
  }
  return $null
}

function Port-Busy($p) {
  try {
    $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction Stop | Select-Object -First 1
    if ($c) {
      $proc = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
      return "PID $($c.OwningProcess) $($proc.ProcessName)"
    }
  } catch { }
  return $null
}

function Check-Port($p, $what) {
  $who = Port-Busy $p
  if ($who) { Die "порт $p занят ($what): $who. Остановите тот процесс или выберите другой порт: .\start.ps1 --port 9000" }
}

function Hash-Of([string[]]$files) {
  $text = ($files | Where-Object { Test-Path $_ } | ForEach-Object { (Get-FileHash $_ -Algorithm SHA256).Hash }) -join ''
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
  $sha = [System.Security.Cryptography.SHA256]::Create()
  return ([BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '').Substring(0, 16)
}

# ---------- зависимости ----------
function Setup-Backend {
  if (-not (Test-Path $Py)) {
    $python = Find-Python
    if (-not $python) { Die 'нужен Python 3.10+: https://www.python.org/downloads/ (при установке отметьте «Add python.exe to PATH»)' }
    Say 'Создаю окружение Python (backend\.venv)...'
    $extra = @($python | Select-Object -Skip 1)
    & $python[0] @extra -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Die 'не удалось создать окружение Python' }
  }
  $want = Hash-Of @((Join-Path $Backend 'requirements.txt'))
  $hashFile = Join-Path $Venv '.requirements-hash'
  $have = if (Test-Path $hashFile) { (Get-Content $hashFile -Raw).Trim() } else { '' }
  if ($want -ne $have) {
    Say 'Ставлю зависимости бэкенда...'
    & $Py -m pip install -q --upgrade pip | Out-Null
    & $Py -m pip install -q -r (Join-Path $Backend 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { Die 'pip не смог поставить зависимости — см. вывод выше' }
    Set-Content -Path $hashFile -Value $want -Encoding ASCII
    Ok 'Зависимости бэкенда установлены'
  }
}

function Setup-Frontend {
  if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
    if ($Mode -eq 'setup') { Warn 'npm не найден — интерфейс не установлен (нужен Node.js 18+: https://nodejs.org)'; return }
    Die 'нужен Node.js 18+ для интерфейса: https://nodejs.org'
  }
  $want = Hash-Of @((Join-Path $Frontend 'package.json'), (Join-Path $Frontend 'package-lock.json'))
  $hashFile = Join-Path $Frontend 'node_modules\.dragapi-hash'
  $have = if (Test-Path $hashFile) { (Get-Content $hashFile -Raw).Trim() } else { '' }
  if (-not (Test-Path (Join-Path $Frontend 'node_modules')) -or $want -ne $have) {
    Say 'Ставлю зависимости интерфейса (npm install)...'
    Push-Location $Frontend
    & npm.cmd install --no-audit --no-fund --loglevel=error
    $code = $LASTEXITCODE
    Pop-Location
    if ($code -ne 0) { Die 'npm install не удался' }
    Set-Content -Path $hashFile -Value $want -Encoding ASCII
    Ok 'Зависимости интерфейса установлены'
  }
}

function Build-Frontend {
  $dist = Join-Path $Frontend 'dist\index.html'
  if (Test-Path $dist) {
    $built = (Get-Item $dist).LastWriteTime
    $changed = Get-ChildItem -Recurse -File (Join-Path $Frontend 'src'), (Join-Path $Frontend 'index.html'), (Join-Path $Frontend 'package.json') |
      Where-Object { $_.LastWriteTime -gt $built } | Select-Object -First 1
    if (-not $changed) { Ok 'Интерфейс собран и не менялся'; return }
  }
  Say 'Собираю интерфейс...'
  Push-Location $Frontend
  & npm.cmd run build --silent | Out-Null
  $code = $LASTEXITCODE
  Pop-Location
  if ($code -ne 0) { Die 'сборка интерфейса не удалась — запустите: cd frontend; npm run build' }
  Ok 'Интерфейс собран'
}

Setup-Backend
Setup-Frontend
if ($Mode -eq 'setup') { Ok 'Всё готово. Запуск: .\start.ps1   (разработка: .\start.ps1 dev)'; exit 0 }

# ---------- запуск ----------
if ($Mode -eq 'prod') {
  Build-Frontend
  Check-Port $Port 'сервер платформы'
  Write-Host ''
  Write-Host "DragAPI -> $Scheme`://localhost:$Port   (Ctrl+C — остановить)" -ForegroundColor Green
  Set-Location $Backend
  & $Py -m app @Rest
  exit $LASTEXITCODE
}

# dev: бэкенд — в отдельном окне (без автоперезапуска: на Windows с ним не работают блоки кода),
# интерфейс — здесь же; Ctrl+C останавливает оба
Check-Port $Port 'бэкенд'
Check-Port $FrontPort 'Vite'
$backendArgs = @('-m', 'app') + $Rest
$server = Start-Process -FilePath $Py -ArgumentList $backendArgs -WorkingDirectory $Backend -PassThru
Write-Host ''
Write-Host 'DragAPI — режим разработки' -ForegroundColor Green
Write-Host "  интерфейс  -> http://localhost:$FrontPort   (правки в frontend\src — сразу)"
Write-Host "  бэкенд     -> $Scheme`://localhost:$Port   (отдельное окно; после правок backend перезапустите его)"
Write-Host ''
try {
  $env:DRAGAPI_BACKEND = "$Scheme`://127.0.0.1:$Port"
  $env:DRAGAPI_FRONT_PORT = "$FrontPort"
  Push-Location $Frontend
  & npx.cmd vite --clearScreen false
} finally {
  Pop-Location
  if ($server -and -not $server.HasExited) { Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue }
  Ok 'Остановлено'
}
