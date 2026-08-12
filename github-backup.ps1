$ErrorActionPreference = "Stop"

$projectDir = $PSScriptRoot
$mutexName = "Local\BooyahBotGitHubBackup"
$mutex = [System.Threading.Mutex]::new($false, $mutexName)
$hasLock = $false

try {
    $hasLock = $mutex.WaitOne(0)
    if (-not $hasLock) {
        exit 0
    }

    Set-Location -LiteralPath $projectDir

    git add --all
    if ($LASTEXITCODE -ne 0) {
        throw "git add failed"
    }

    git diff --cached --quiet
    $diffExit = $LASTEXITCODE
    if ($diffExit -eq 0) {
        exit 0
    }
    if ($diffExit -ne 1) {
        throw "git diff failed"
    }

    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss K"
    git commit -m "Automatic backup $stamp"
    if ($LASTEXITCODE -ne 0) {
        throw "git commit failed"
    }

    git push origin main
    if ($LASTEXITCODE -ne 0) {
        throw "git push failed"
    }
}
finally {
    if ($hasLock) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
