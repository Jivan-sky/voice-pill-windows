# 一键编插件 exe。GOPROXY 固定成 goproxy.cn：SDK 的间接依赖在本机走 IPv6 会卡死（实测）。
# 这个文件必须存成 UTF-8 **带 BOM**。PowerShell 5.1 读无 BOM 的 .ps1 会按
# ANSI(GBK) 解释：注释里的中文一旦被解错，就能把后面那行 go build 吞进注释里，
# 表现出来是"go build 退出码 "（$LASTEXITCODE 空的）这句莫名其妙的报错。
# 2026-10-06 实测踩到；同一个坑 autostart.py 生成 .ps1 时也写了（utf-8-sig）。
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$env:GOPROXY = "https://goproxy.cn,direct"
Push-Location "$root\go\voicepill"
try {
    # -H windowsgui：GUI 子系统。Windows **不会**给这个进程建控制台，所以哨兵从
    # HKCU\...\Run 被拉起时不再有那个黑窗口（连"先建后藏"那一闪也没有）。
    # 这一条是量过才改的（2026-10-06）：钩子那条路（cmd /c hook.cmd + 全管道）
    # 两种构建都照等、stdin/stdout/退出码都通，80 项 exe 自测对 GUI 构建全过。
    # 详见 go/voicepill/internal/sentinel/console.go 顶上那段。
    go build -trimpath -ldflags "-s -w -H windowsgui" -o "$root\bin\voicepill.exe" .
    if ($LASTEXITCODE -ne 0) { throw "go build 退出码 $LASTEXITCODE" }
} finally {
    Pop-Location
}
$exe = Get-Item "$root\bin\voicepill.exe"
"OK {0} bytes" -f $exe.Length