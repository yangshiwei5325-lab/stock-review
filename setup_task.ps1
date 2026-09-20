# ============================================================
#  A股每日复盘 —— 注册 Windows 定时任务
#  每周一到周五 15:30 自动生成复盘并推送到飞书
#  如需卸载： Unregister-ScheduledTask -TaskName 'A股每日复盘推送' -Confirm:$false
# ============================================================
$ErrorActionPreference = 'Stop'

$taskName = 'A股每日复盘推送'
$batPath  = 'F:\DeepSeek\stock-review\每日复盘推送.bat'
$workDir  = 'F:\DeepSeek\stock-review'

if (-not (Test-Path $batPath)) {
    Write-Host "[错误] 找不到 $batPath" -ForegroundColor Red
    exit 1
}

$action = New-ScheduledTaskAction -Execute $batPath -WorkingDirectory $workDir

# 周一到周五 15:30（收盘后 30 分钟，数据已定稿）
$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At '15:30'

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5)

# Interactive = 仅在你登录时运行（不需要存密码，也不碰 SYSTEM 账户的代理问题）
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "[OK] 定时任务已注册：$taskName" -ForegroundColor Green
$t = Get-ScheduledTask -TaskName $taskName
$i = Get-ScheduledTaskInfo -TaskName $taskName
Write-Host ("     状态     : {0}" -f $t.State)
Write-Host ("     下次运行 : {0}" -f $i.NextRunTime)
Write-Host ("     执行     : {0}" -f $batPath)
