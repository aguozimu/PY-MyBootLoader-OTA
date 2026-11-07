$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Test-PythonCandidate([string]$Path) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $false }
    try {
        & $Path -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

$python = $null
if (Test-Path ".venv\Scripts\python.exe") {
    $python = (Resolve-Path ".venv\Scripts\python.exe").Path
} else {
    $candidates = @()
    foreach ($entry in (Get-Command python -All -ErrorAction SilentlyContinue)) {
        if ($entry.Source -and $entry.Source -notlike "*WindowsApps*") { $candidates += $entry.Source }
    }
    $candidates += Get-ChildItem "$env:LOCALAPPDATA\Python\pythoncore-*\python.exe" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName
    $candidates += Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python*\python.exe" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName
    $candidates += Get-ChildItem "C:\Python*\python.exe" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName
    $python = $candidates | Select-Object -Unique | Where-Object { Test-PythonCandidate $_ } | Select-Object -First 1
}

if (-not $python) {
    throw "未找到可用的 Python 3.10+。请安装 Python 并勾选 Add Python to PATH。"
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "[1/3] 创建虚拟环境..."
    & $python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "虚拟环境创建失败" }
}

$venvPython = (Resolve-Path ".venv\Scripts\python.exe").Path
& $venvPython -c "import serial, clr" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[2/3] 安装运行依赖..."
    & $venvPython -m pip install --no-cache-dir -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "依赖安装失败" }
} else {
    Write-Host "[2/3] 依赖已就绪"
}

Write-Host "[3/3] 启动 OTA Console 桌面软件..."
$venvPythonw = Join-Path (Split-Path $venvPython) "pythonw.exe"
if (-not (Test-Path $venvPythonw)) { $venvPythonw = $venvPython }
$appProcess = Start-Process -FilePath $venvPythonw -ArgumentList "-m", "ota_host" -WorkingDirectory $PSScriptRoot -PassThru
Start-Sleep -Milliseconds 1800
$appProcess.Refresh()
if ($appProcess.HasExited) {
    throw "上位机启动失败，请查看 logs/runtime 下的运行日志。"
}
Write-Host "OTA Console 已启动，进程 PID=$($appProcess.Id)"



