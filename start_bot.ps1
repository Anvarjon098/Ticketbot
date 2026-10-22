# Botni ko'rinmas holda ishga tushiradi va kuzatib turadi: yiqilsa, 10 soniyadan keyin qayta ishga tushiradi.
# Kompyuter yoqilganda Windows "Avtozagruzka" papkasidagi UzRailBot.vbs orqali o'zi ishga tushadi.
# To'xtatish: stop_bot.ps1
Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"

# Avvalgi nusxa ishlayotgan bo'lsa — to'xtatamiz (ikkita bot bir vaqtda ishlamasin)
& "$PSScriptRoot\stop_bot.ps1"

$PID | Out-File watchdog.pid -Encoding ascii
while ($true) {
    if (Test-Path bot.log) { Move-Item bot.log bot.prev.log -Force }
    $p = Start-Process -FilePath ".\.venv\Scripts\python.exe" -ArgumentList "bot.py" -WorkingDirectory $PSScriptRoot `
        -WindowStyle Hidden -RedirectStandardError "bot.log" -RedirectStandardOutput "bot.out.log" -PassThru
    $p.Id | Out-File bot.pid -Encoding ascii
    $p.WaitForExit()
    Start-Sleep -Seconds 10
}
