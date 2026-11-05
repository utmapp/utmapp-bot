# Wait until a top-level window whose title starts with -Title exists, then
# write -Out.  Window titles are only visible from the interactive session,
# so this runs there (as a console task), and the host polls -Out over SSH.
param([string]$Title, [string]$Out, [int]$Seconds = 600)
Remove-Item $Out -ErrorAction SilentlyContinue
$end = (Get-Date).AddSeconds($Seconds)
while ((Get-Date) -lt $end) {
    $p = Get-Process | Where-Object { $_.MainWindowTitle -like "$Title*" } | Select-Object -First 1
    if ($p) { "UP $($p.ProcessName) $($p.Id)" | Out-File -Encoding ascii $Out; exit 0 }
    Start-Sleep -Milliseconds 500
}
"TIMEOUT" | Out-File -Encoding ascii $Out
exit 1
