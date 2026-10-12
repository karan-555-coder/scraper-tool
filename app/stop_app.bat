@echo off
rem Stops the Business Data Collector (visible or hidden instance).
rem Progress is always saved - collections resume on next start.
rem The web process is stopped first; its collection worker then stops every
rem running collection, writes the final checkpoint and exits by itself -
rem this script waits for that (up to 40 s) so a quick restart can never
rem race the final save. Only the app's own process is stopped: another
rem program that happens to listen on port 8100 is left alone.
cd /d "%~dp0"
echo Stopping Business Data Collector...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$found = $false;" ^
  "foreach ($c in Get-NetTCPConnection -LocalPort 8100 -State Listen -ErrorAction SilentlyContinue) {" ^
  "  $p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $c.OwningProcess);" ^
  "  if (-not $p -or $p.CommandLine -notlike '*backend.serve*') { Write-Host ('Port 8100 is used by another program (PID ' + $c.OwningProcess + ') - left running.'); continue }" ^
  "  $found = $true; $api = $p.ProcessId;" ^
  "  Stop-Process -Id $api -Force -ErrorAction SilentlyContinue;" ^
  "  Write-Host 'Saving collection progress...';" ^
  "  $t = (Get-Date).AddSeconds(40);" ^
  "  while ((Get-Date) -lt $t -and (Get-CimInstance Win32_Process -Filter ('CommandLine like ''%%parent_pid=' + $api + ',%%''') )) { Start-Sleep -Milliseconds 500 }" ^
  "};" ^
  "if (-not $found) { Write-Host 'The app was not running.' }" ^
  "$lock = Join-Path (Get-Location) 'output\app.lock';" ^
  "if (Test-Path $lock) { $o = [int]((Get-Content $lock -Raw) -replace '\D',''); if ($o -le 0 -or -not (Get-Process -Id $o -ErrorAction SilentlyContinue)) { Remove-Item $lock -ErrorAction SilentlyContinue } }"
echo Stopped. Start it again with the Desktop shortcut or start_app.bat.
ping -n 2 127.0.0.1 >nul
