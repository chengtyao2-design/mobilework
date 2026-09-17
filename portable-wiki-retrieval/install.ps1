param(
  [ValidateSet("opencode", "codex", "claude-code", "all")][string]$Client = "all",
  [string]$Project = (Get-Location).Path
)
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
python "$PSScriptRoot\scripts\install.py" --client $Client --project $Project
