# Memory Lane - prepares the private Python environment. Run by the installer; safe to run again.
param([string]$InstallDir)
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}
function Say($t) { Write-Host $t }

function Find-Python {
    foreach ($v in "3.13", "3.12", "3.11", "3.10") {
        try {
            $p = (& py "-$v" -c "import sys;print(sys.executable)" 2>$null)
            if ($LASTEXITCODE -eq 0 -and $p) { $p = "$p".Trim(); if (Test-Path $p) { return $p } }
        } catch {}
    }
    try {
        $p = (& python -c "import sys;print(sys.executable if (3,10)<=sys.version_info[:2]<=(3,13) else '')" 2>$null)
        if ($p) { $p = "$p".Trim(); if ($p -and (Test-Path $p)) { return $p } }
    } catch {}
    foreach ($d in "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe") { if (Test-Path $d) { return $d } }
    return $null
}

Say "Looking for Python..."
$py = Find-Python
if (-not $py) {
    Say "Python is not on this computer yet. Downloading it (about 25 MB)..."
    $url = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
    $exe = Join-Path $env:TEMP "python-3.12.10-amd64.exe"
    try {
        Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
        Say "Installing Python for this Windows user only (no administrator needed)..."
        Start-Process -FilePath $exe -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=0", "Include_launcher=0", "Include_test=0", "Shortcuts=0" -Wait
    } catch {
        Say "Could not download or install Python: $($_.Exception.Message)"
    }
    $py = Find-Python
}
if (-not $py) {
    Say "ERROR: Python could not be set up. Check the internet connection and run this Setup again."
    exit 2
}
Say "Using Python at $py"

$venv = Join-Path $InstallDir "venv"
$vpy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $vpy)) {
    Say "Creating Memory Lane's own private Python folder..."
    & $py -m venv $venv
}
if (-not (Test-Path $vpy)) {
    Say "ERROR: could not create the private Python folder."
    exit 3
}

$app = Join-Path $InstallDir "app"
Say "Updating the installer tools..."
& $vpy -m pip install --disable-pip-version-check -q --upgrade pip 2>&1 | ForEach-Object { Say $_ }

Say "Installing the basic parts (photos and iPhone pictures)..."
& $vpy -m pip install --disable-pip-version-check -q -r (Join-Path $app "requirements.txt") 2>&1 | ForEach-Object { Say $_ }
if ($LASTEXITCODE -ne 0) {
    Say "ERROR: the basic parts could not be installed. Check the internet connection and run this Setup again."
    exit 4
}

$optional = @(
    @("requirements-places.txt", "photo locations (places)"),
    @("requirements-faces.txt",  "face finding"),
    @("requirements-ai.txt",     "smart tags (AI) - this one is large and can take a few minutes")
)
foreach ($o in $optional) {
    Say ("Installing " + $o[1] + "...")
    & $vpy -m pip install --disable-pip-version-check -q -r (Join-Path $app $o[0]) 2>&1 | ForEach-Object { Say $_ }
    if ($LASTEXITCODE -ne 0) { Say ("NOTE: " + $o[1] + " could not be installed. The rest of Memory Lane still works. Run Setup again later to retry.") }
}

Say "Checking everything..."
& $vpy (Join-Path $app "photokeeper.py") --check 2>&1 | ForEach-Object { Say $_ }
exit 0
