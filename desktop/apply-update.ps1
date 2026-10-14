$ErrorActionPreference = 'Stop'
$source = $env:WB_UPDATE_SOURCE
$target = $env:WB_UPDATE_TARGET
$backup = "$target.previous"
$temporary = "$target.new"
$taskLog = $env:WB_UPDATE_LOG

# $replaced 必须在 try 之前初始化：以前只在 try 内部赋值，一旦在
# `[int]$env:WB_UPDATE_PID` 处抛错（env 缺失/非数字），catch 里读 $replaced
# 就会取到未定义值，回滚判断被静默跳过。
$replaced = $false

function Write-TaskLog([string]$Message) {
    if ($taskLog) {
        Add-Content -LiteralPath $taskLog -Value "$(Get-Date -Format o) $Message"
    }
}

try {
    # env 变量缺失/非数字时不要抛：没有 PID 就意味着"等不到旧进程退出"，
    # 后续的重试复制循环本身已经能处理文件被占用的情况。
    $pidValue = 0
    [void][int]::TryParse($env:WB_UPDATE_PID, [ref]$pidValue)
    if ($pidValue -gt 0) {
        $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
        if ($process) { $process.WaitForExit() }
    }

    # 目标必须是"已知安装位置"，且不能是符号链接/重解析点 ——
    # 否则 $target 可能被换成指向别处的链接，让我们把新 EXE 写到攻击者选择的位置。
    $targetFull = [System.IO.Path]::GetFullPath($target)
    if (Test-Path -LiteralPath $targetFull) {
        $item = Get-Item -LiteralPath $targetFull -Force
        if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            throw "目标是可重解析点（符号链接），拒绝替换：$targetFull"
        }
    }

    # 先把新文件复制到临时路径，并**记录其哈希**；执行前再校验一次，
    # 关闭 preflight（desktop/app.py 探测可写）与真正执行之间的 TOCTOU 窗口。
    Copy-Item -LiteralPath $source -Destination $temporary -Force
    $expectedHash = (Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash

    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            Copy-Item -LiteralPath $target -Destination $backup -Force
            # 替换前复核临时文件未被篡改
            $actualHash = (Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash
            if ($actualHash -ne $expectedHash) {
                throw '待替换的文件在等待期间被修改，已中止（防止执行被替换的二进制）。'
            }
            Move-Item -LiteralPath $temporary -Destination $targetFull -Force
            $replaced = $true
            break
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $replaced) { throw '无法替换 EXE，已保留当前版本。' }

    # 执行前最后一次确认目标哈希 == 预期哈希（此时 target 就是刚移过去的文件，
    # 若中途被第三方替换则拒绝执行）。
    $finalHash = (Get-FileHash -LiteralPath $targetFull -Algorithm SHA256).Hash
    if ($finalHash -ne $expectedHash) {
        throw '替换后的文件哈希不符，拒绝执行。'
    }

    # New onefile must extract its own libraries, rather than reusing the old bundle.
    $env:PYINSTALLER_RESET_ENVIRONMENT = '1'
    Start-Process -FilePath $targetFull -WorkingDirectory (Split-Path -Parent $targetFull) -WindowStyle Hidden
    Write-TaskLog "更新成功，旧版备份：$backup"
} catch {
    Write-TaskLog "更新失败：$_"
    if ($replaced -and (Test-Path -LiteralPath $backup)) {
        Copy-Item -LiteralPath $backup -Destination $target -Force
    }
    $env:PYINSTALLER_RESET_ENVIRONMENT = '1'
    if (Test-Path -LiteralPath $target) { Start-Process -FilePath $target -WindowStyle Hidden }
}
