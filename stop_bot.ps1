# Botni va uni kuzatib turuvchi skriptni (start_bot.ps1) to'xtatadi.
Set-Location $PSScriptRoot
foreach ($file in "watchdog.pid", "bot.pid") {
    if (Test-Path $file) {
        $id = Get-Content $file -ErrorAction SilentlyContinue
        if ($id -and $id -ne $PID) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
        Remove-Item $file -ErrorAction SilentlyContinue
    }
}
# eski versiyadagi vaqtinchalik tunnel qolib ketgan bo'lsa
Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
