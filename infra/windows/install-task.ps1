# Register the bash-mcp Windows Scheduled Task.
# This task runs on Windows login (60s delay) and invokes the WSL-side launcher.sh,
# which ensures bash-mcp.service is started so the MCP server is reachable.
#
# Run as Administrator from PowerShell:
#   .\install-task.ps1
# Or with a custom task name / repo path:
#   .\install-task.ps1 -TaskName "BashMcp-WSL-Bootstrap" -RepoPath "/home/jbecerra/projects/bash-mcp"

[CmdletBinding()]
param(
    [string]$TaskName = "BashMcp-WSL-Bootstrap",
    [string]$RepoPath = "/home/jbecerra/projects/bash-mcp"
)

$ErrorActionPreference = 'Stop'

# Resolve the XML template shipped with this file
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$xmlPath = Join-Path $scriptDir "$TaskName.xml"
if (-not (Test-Path $xmlPath)) {
    Write-Error "XML template not found: $xmlPath"
    exit 1
}

# Read the template, swap the repo path in <Arguments>
$xml = Get-Content $xmlPath -Raw
$xml = $xml -replace '/home/jbecerra/projects/bash-mcp/infra/launcher\.sh', "$RepoPath/infra/launcher.sh"

$action = New-ScheduledTaskAction `
    -Execute "wsl.exe" `
    -Argument "$RepoPath/infra/launcher.sh"

$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

# Replace any pre-existing task with the same name
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Removing existing task: $TaskName"
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Bootstraps bash-mcp on Windows login (runs WSL-side launcher.sh to start bash-mcp.service)" `
    -User "$env:USERNAME" `
    -RunLevel Highest

Write-Host "OK: registered Scheduled Task '$TaskName'"
Write-Host "  Triggers:  AtLogOn + 60s delay"
Write-Host "  Action:    wsl.exe $RepoPath/infra/launcher.sh"
Write-Host ""
Write-Host "Verify:"
Write-Host "  Get-ScheduledTask -TaskName $TaskName | Format-List"
Write-Host ""
Write-Host "To remove:"
Write-Host "  Unregister-ScheduledTask -TaskName $TaskName -Confirm:\$false"
