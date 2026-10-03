# 一键编插件 exe。GOPROXY 固定成 goproxy.cn：SDK 的间接依赖在本机走 IPv6 会卡死（实测）。
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$env:GOPROXY = "https://goproxy.cn,direct"
Push-Location "$root\go\voicepill"
try {
    go build -trimpath -ldflags "-s -w" -o "$root\bin\voicepill.exe" .
    if ($LASTEXITCODE -ne 0) { throw "go build 退出码 $LASTEXITCODE" }
} finally {
    Pop-Location
}
$exe = Get-Item "$root\bin\voicepill.exe"
"OK {0} bytes" -f $exe.Length