$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Bat = Join-Path $ProjectDir "executar_automatico.bat"

$Action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"$Bat`"" `
    -WorkingDirectory $ProjectDir

$Trigger = New-ScheduledTaskTrigger -Daily -At 3:00AM

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 30)

Register-ScheduledTask `
    -TaskName "NFSe Download Automatico 03h" `
    -Description "Baixa XMLs do dia anterior, gera DANFSe e copia PDFs para a rede." `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Force

Write-Host "Tarefa criada com sucesso."
