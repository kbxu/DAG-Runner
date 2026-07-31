# This public example is intentionally environment-neutral.
$env:PYTHONUNBUFFERED = "1"

Set-Location "workdir\path"
$CondaExe = "path\conda.exe"
$CondaEnv = "environment"

if ([IO.Path]::IsPathRooted($CondaEnv)) {
    $CondaEnvPath = $CondaEnv
} elseif ($CondaEnv -eq "base") {
    $CondaEnvPath = (& $CondaExe info --base).Trim()
} else {
    $CondaEnvPath = ((& $CondaExe env list --json | ConvertFrom-Json).envs |
        Where-Object { (Split-Path $_ -Leaf) -eq $CondaEnv } |
        Select-Object -First 1)
}
if (-not $CondaEnvPath -or -not (Test-Path -LiteralPath $CondaEnvPath -PathType Container)) {
    throw "Conda environment not found: $CondaEnv"
}

$CondaPathEntries = @(
    $CondaEnvPath
    (Join-Path $CondaEnvPath "Library\mingw-w64\bin")
    (Join-Path $CondaEnvPath "Library\usr\bin")
    (Join-Path $CondaEnvPath "Library\bin")
    (Join-Path $CondaEnvPath "Scripts")
    (Join-Path $CondaEnvPath "bin")
)
$env:PATH = (($CondaPathEntries + $env:PATH) -join [IO.Path]::PathSeparator)
$env:CONDA_PREFIX = $CondaEnvPath
$env:CONDA_DEFAULT_ENV = $CondaEnv
$env:CONDA_EXE = $CondaExe
