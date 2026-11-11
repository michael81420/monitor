param(
    [ValidateSet('Run', 'Status', 'Stop')][string]$Action = 'Run',
    [Parameter(Mandatory=$true)][string]$Key,
    [string]$DataDir,
    [string]$Python,
    [string]$Project,
    [string]$SnapshotFile,
    [string]$Evidence,
    [switch]$Verify
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$prefix = 'Local\MonitorBot.Tray.' + $Key
if ($Action -ne 'Run') {
    $ready = $null
    try { $ready = [Threading.EventWaitHandle]::OpenExisting($prefix + '.ready') } catch [Threading.WaitHandleCannotBeOpenedException] {}
    $loaded = $null -ne $ready -and $ready.WaitOne(0)
    if ($Action -eq 'Stop' -and $loaded) {
        $stop = [Threading.EventWaitHandle]::OpenExisting($prefix + '.stop')
        try { $null = $stop.Set() } finally { $stop.Dispose() }
    }
    if ($null -ne $ready) { $ready.Dispose() }
    @{loaded=[bool]$loaded} | ConvertTo-Json -Compress
    exit 0
}
$created = $false
$mutex = [Threading.Mutex]::new($true, $prefix + '.mutex', [ref]$created)
if (-not $created) { $mutex.Dispose(); exit 0 }
$ready = [Threading.EventWaitHandle]::new($false, [Threading.EventResetMode]::ManualReset, $prefix + '.ready')
$stop = [Threading.EventWaitHandle]::new($false, [Threading.EventResetMode]::ManualReset, $prefix + '.stop')
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()
$script:context = New-Object Windows.Forms.ApplicationContext
$script:menu = New-Object Windows.Forms.ContextMenuStrip
$script:menu.Font = New-Object Drawing.Font('Microsoft JhengHei UI', 10)
$script:icon = New-Object Windows.Forms.NotifyIcon
$script:icon.Icon = [Drawing.SystemIcons]::Information
$script:icon.Text = 'Monitor：正在讀取狀態'
$script:icon.ContextMenuStrip = $script:menu
$script:snapshot = $null
$script:refreshState = 'loading'
$script:job = $null
$script:pendingRefresh = $true
$script:lastRefresh = [DateTime]::MinValue
$script:verifyStage = 0
$script:verifyTicks = 0
$script:checks = New-Object Collections.Generic.List[string]
$script:verifyError = $null

function Add-Row([string]$text, [scriptblock]$click = $null, $parent = $script:menu) {
    $row = New-Object Windows.Forms.ToolStripMenuItem($text)
    $row.Enabled = $null -ne $click
    if ($null -ne $click) { $row.add_Click($click) }
    $null = $parent.Items.Add($row)
    return $row
}
function Add-Child($parent, [string]$text) {
    $row = New-Object Windows.Forms.ToolStripMenuItem($text)
    $row.Enabled = $false
    $row.ToolTipText = $text
    if ($text.Length -gt 65) { $row.Text = $text.Substring(0, 65) + '…' }
    $null = $parent.DropDownItems.Add($row)
}
function Time-Label($value) {
    if (-not $value) { return '尚未收集' }
    try { return ([DateTimeOffset]::Parse($value).ToLocalTime().ToString('MM/dd HH:mm:ss')) }
    catch { return [string]$value }
}
function Quote-Argument([string]$value) {
    return '"' + [regex]::Replace([regex]::Replace($value, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
}
function Start-Command([string[]]$arguments, [bool]$capture = $false) {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $Python
    $parts = @('-m', 'monitorbot', '--home', $DataDir) + $arguments
    $info.Arguments = ($parts | ForEach-Object { Quote-Argument $_ }) -join ' '
    $info.WorkingDirectory = $Project
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    if ($capture) {
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        $info.StandardOutputEncoding = New-Object Text.UTF8Encoding($false)
        $info.StandardErrorEncoding = New-Object Text.UTF8Encoding($false)
    }
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    $null = $process.Start()
    return $process
}
function Launch-Action([string[]]$arguments) {
    try { $process = Start-Command $arguments; $process.Dispose() }
    catch { [Windows.Forms.MessageBox]::Show('無法執行操作，請確認 Python 與專案路徑。', 'Monitor') | Out-Null }
}
function Build-Menu {
    if ($script:menu.Visible) { $script:menuDirty = $true; return }
    $script:menuDirty = $false
    while ($script:menu.Items.Count -gt 0) {
        $item = $script:menu.Items[0]
        $script:menu.Items.RemoveAt(0)
        $item.Dispose()
    }
    $null = Add-Row 'Monitor 工作助理'
    if ($script:refreshState -eq 'failed') {
        $null = Add-Row $(if ($null -eq $script:snapshot) { '無法讀取狀態，請重新整理' } else { '狀態更新失敗，顯示上次資料' })
        $script:icon.Icon = [Drawing.SystemIcons]::Error
    } elseif ($script:refreshState -eq 'loading') {
        $null = Add-Row '正在讀取狀態…'
    }
    $s = $script:snapshot
    if ($null -ne $s) {
        $running = [bool]$s.background.loaded
        $stateLabel = if ($running) { '背景收集執行中' } else { '背景收集未啟用' }
        $null = Add-Row $stateLabel
        $null = Add-Row ('最後收集：' + (Time-Label $s.last_poll))
        $tasks = @($s.tasks)
        $active = @($tasks | Where-Object { $_.status -ne 'waiting' })
        $waiting = @($tasks | Where-Object { $_.status -eq 'waiting' })
        $null = Add-Row ('待辦 {0} 項 · 等待 {1} 項' -f $active.Count, $waiting.Count)
        $null = Add-Row ('待確認 {0} 題 · 尚未分析 {1} 筆' -f $s.question_count, $s.pending_events)
        $sources = New-Object Windows.Forms.ToolStripMenuItem('來源狀態')
        $null = $script:menu.Items.Add($sources)
        $issues = 0
        foreach ($property in $s.source_health.PSObject.Properties) {
            $health = $property.Value
            $labels = @{ok='正常'; excluded='已停用'; unconfigured='尚未設定'; unavailable='無法使用'; error='錯誤'; permission_denied='權限不足'}
            $label = if ($labels.ContainsKey([string]$health.status)) { $labels[[string]$health.status] } else { [string]$health.status }
            if ($health.status -notin @('ok', 'excluded')) { $issues++ }
            Add-Child $sources ($property.Name + '：' + $label + $(if ($health.detail) { '，' + $health.detail } else { '' }))
        }
        if ($sources.DropDownItems.Count -eq 0) { Add-Child $sources '來源尚未檢查' }
        if ($tasks.Count -gt 0) {
            $taskMenu = New-Object Windows.Forms.ToolStripMenuItem('目前待辦')
            $null = $script:menu.Items.Add($taskMenu)
            foreach ($task in ($tasks | Select-Object -First 15)) {
                $title = [string]$task.title
                if ($title.Length -gt 48) { $title = $title.Substring(0, 48) + '…' }
                Add-Child $taskMenu ($title + $(if ($task.next_action) { '，下一步：' + $task.next_action } else { '' }))
            }
        }
        if ($script:refreshState -ne 'failed') {
            $script:icon.Icon = if (-not $running) { [Drawing.SystemIcons]::Application } elseif ($issues -gt 0) { [Drawing.SystemIcons]::Warning } else { [Drawing.SystemIcons]::Information }
        }
        $tip = 'Monitor：' + $stateLabel + '，待辦 ' + $tasks.Count + ' 項'
        if ($script:refreshState -eq 'failed') { $tip = 'Monitor：狀態更新失敗，資料可能已過期' }
        $script:icon.Text = $tip.Substring(0, [Math]::Min(63, $tip.Length))
    } elseif ($script:refreshState -eq 'failed') {
        $script:icon.Text = 'Monitor：無法讀取狀態'
    }
    $null = $script:menu.Items.Add((New-Object Windows.Forms.ToolStripSeparator))
    $script:refreshRow = Add-Row '重新整理' { $script:pendingRefresh = $true }
    $null = Add-Row '開啟每日清單' { Launch-Action @('menubar', 'todo') }
    $null = Add-Row '每日確認' { Launch-Action @('review') }
    $null = Add-Row '新增待辦' { Launch-Action @('menubar', 'add') }
    $null = $script:menu.Items.Add((New-Object Windows.Forms.ToolStripSeparator))
    $script:quitRow = Add-Row '退出狀態圖示' { $script:context.ExitThread() }
}
function Apply-Snapshot([string]$text) {
    $value = $text | ConvertFrom-Json
    if ($null -eq $value -or $null -eq $value.background -or $null -eq $value.tasks -or
        $null -eq $value.question_count -or $null -eq $value.pending_events -or
        $null -eq $value.source_health -or $value.background.loaded -isnot [bool]) {
        throw 'Invalid menu snapshot'
    }
    $script:snapshot = $value
    $script:refreshState = 'ready'
}
function Refresh-Tick {
    if ($null -ne $script:job) {
        if (-not $script:job.Process.HasExited -and ([DateTime]::UtcNow - $script:job.Started).TotalSeconds -lt 35) { return }
        try {
            if (-not $script:job.Process.HasExited) { $script:job.Process.Kill(); throw 'Snapshot timed out' }
            if ($script:job.Process.ExitCode -ne 0) { throw 'Snapshot command failed' }
            Apply-Snapshot ($script:job.Output.GetAwaiter().GetResult())
        } catch { $script:refreshState = 'failed' }
        finally { $script:job.Process.Dispose(); $script:job = $null }
        Build-Menu
    }
    if (-not $script:pendingRefresh -and ([DateTime]::UtcNow - $script:lastRefresh).TotalSeconds -lt 60) { return }
    $script:pendingRefresh = $false
    $script:lastRefresh = [DateTime]::UtcNow
    try {
        if ($SnapshotFile) {
            Apply-Snapshot ([IO.File]::ReadAllText($SnapshotFile, [Text.Encoding]::UTF8))
            Build-Menu
        } else {
            $process = Start-Command @('menubar', 'snapshot') $true
            $script:job = @{Process=$process; Output=$process.StandardOutput.ReadToEndAsync(); Errors=$process.StandardError.ReadToEndAsync(); Started=[DateTime]::UtcNow}
        }
    } catch { $script:refreshState = 'failed'; Build-Menu }
}
function Assert-Check([bool]$condition, [string]$label) {
    if (-not $condition) { throw $label }
    $script:checks.Add($label)
}
function Verify-Tick {
    $script:verifyTicks++
    if ($script:verifyTicks -gt 100) { throw 'Verification did not finish' }
    switch ($script:verifyStage) {
        0 {
            if ($script:refreshState -ne 'ready') { return }
            Assert-Check $script:icon.Visible 'real NotifyIcon is visible'
            $script:original = [IO.File]::ReadAllText($SnapshotFile, [Text.Encoding]::UTF8)
            $script:originalCount = @($script:snapshot.tasks).Count
            $script:menu.Show(80, 80)
            $script:verifyStage = 10
        }
        10 {
            Assert-Check $script:menu.Visible 'real ContextMenuStrip opens'
            Assert-Check (@($script:menu.Items | Where-Object { $_.Text -like '待辦*' }).Count -eq 1) 'Unicode status rows are present'
            if ($Evidence) {
                $bitmap = New-Object Drawing.Bitmap($script:menu.Width, $script:menu.Height)
                $graphics = [Drawing.Graphics]::FromImage($bitmap)
                try {
                    $graphics.CopyFromScreen($script:menu.Left, $script:menu.Top, 0, 0, $bitmap.Size)
                    $bitmap.Save($Evidence + '.png', [Drawing.Imaging.ImageFormat]::Png)
                } finally { $graphics.Dispose(); $bitmap.Dispose() }
            }
            $script:menu.Close()
            [IO.File]::WriteAllText($SnapshotFile, '{broken', (New-Object Text.UTF8Encoding($false)))
            $script:refreshRow.PerformClick()
            $script:verifyStage = 1
        }
        1 {
            if ($script:refreshState -ne 'failed') { return }
            Assert-Check (@($script:snapshot.tasks).Count -eq $script:originalCount) 'refresh failure preserves previous tasks'
            Assert-Check (@($script:menu.Items | Where-Object { $_.Text -eq '狀態更新失敗，顯示上次資料' }).Count -eq 1) 'refresh failure is visible'
            $changed = $script:original | ConvertFrom-Json
            $changed.tasks = @()
            $changed.background.loaded = $false
            [IO.File]::WriteAllText($SnapshotFile, ($changed | ConvertTo-Json -Depth 20), (New-Object Text.UTF8Encoding($false)))
            $script:refreshRow.PerformClick()
            $script:verifyStage = 2
        }
        2 {
            if ($script:refreshState -ne 'ready') { return }
            Assert-Check (@($script:menu.Items | Where-Object { $_.Text -eq '背景收集未啟用' }).Count -eq 1) 'refresh action updates collector state'
            Assert-Check (@($script:menu.Items | Where-Object { $_.Text -eq '待辦 0 項 · 等待 0 項' }).Count -eq 1) 'refresh action updates task counts'
            $script:checks.Add('quit action invoked on real menu item')
            $script:quitRow.PerformClick()
            $script:verifyStage = 3
        }
    }
}
$timer = New-Object Windows.Forms.Timer
$timer.Interval = 150
$timer.add_Tick({
    try {
        if ($stop.WaitOne(0)) { $script:context.ExitThread(); return }
        $null = $ready.Set()
        Refresh-Tick
        if ($Verify) { Verify-Tick }
    } catch {
        if ($Verify) { $script:verifyError = $_.Exception.Message; $script:context.ExitThread() }
        else { $script:refreshState = 'failed'; Build-Menu }
    }
})
$script:menu.add_Opening({ $script:pendingRefresh = $true })
$script:menu.add_Closed({ if ($script:menuDirty) { Build-Menu } })
$script:icon.add_MouseUp({ param($sender, $eventArgs)
    if ($eventArgs.Button -eq [Windows.Forms.MouseButtons]::Left) { $script:menu.Show([Windows.Forms.Cursor]::Position) }
})
try {
    Build-Menu
    $script:icon.Visible = $true
    $timer.Start()
    [Windows.Forms.Application]::Run($script:context)
} finally {
    $null = $ready.Reset()
    $timer.Stop(); $timer.Dispose()
    if ($null -ne $script:job) {
        if (-not $script:job.Process.HasExited) { $script:job.Process.Kill() }
        $script:job.Process.Dispose()
    }
    $script:icon.Visible = $false
    $script:icon.Dispose(); $script:menu.Dispose(); $script:context.Dispose()
    $stop.Dispose(); $ready.Dispose()
    $mutex.ReleaseMutex(); $mutex.Dispose()
    if ($Verify -and $Evidence) {
        @{passed=@($script:checks.ToArray()); error=$script:verifyError; completed=($script:verifyStage -eq 3)} | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 -LiteralPath $Evidence
    }
}
if ($script:verifyError) { throw $script:verifyError }
