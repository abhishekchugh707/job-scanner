# Starts Job Radar at Windows logon (shortcut in your Startup folder, no admin rights needed).
#   setup_autostart.bat   -> install + start now
#   remove_autostart.bat  -> remove + stop
param([switch]$Remove)
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'Job Radar.lnk'

# stop a running copy
Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
  Where-Object { $_.CommandLine -like '*job-finder*app.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

if ($Remove) {
  if (Test-Path $lnk) { Remove-Item $lnk }
  Write-Host 'Job Radar removed from startup and stopped.'
  exit 0
}

$py = (& python -c "import sys; print(sys.executable)").Trim()
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'
if (-not (Test-Path $pyw)) { Write-Host "pythonw.exe not found next to $py"; exit 1 }

$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = $pyw
$s.Arguments = '"' + (Join-Path $dir 'app.py') + '"'
$s.WorkingDirectory = $dir
$s.Description = 'Job Radar - hourly job scan + dashboard on http://localhost:8765'
$s.Save()

Start-Process -FilePath $pyw -ArgumentList ('"' + (Join-Path $dir 'app.py') + '"', '--open') -WorkingDirectory $dir
Write-Host 'Job Radar starts with Windows from now on, and is running: http://localhost:8765'
