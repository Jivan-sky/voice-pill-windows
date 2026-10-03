#!/usr/bin/env bash
# 编译两个 ASR 引擎到 bin/。在 Git Bash 里跑。
#
#   bash tools/build-engines.sh            # 两个都编
#   bash tools/build-engines.sh codex      # 只编 codex-asr
#   bash tools/build-engines.sh freeasr    # 只编 freeasr
#
# 前置（都已装好，见 docs/移植方案.md 第 3 节）：
#   - MSYS2 的 mingw-w64-x86_64-gcc / -opus / -opusfile / -pkg-config
#   - rustup 工具链 stable-x86_64-pc-windows-gnu
#   - Go 1.27+
#
# 两个坑都写在注释里了，改脚本前先读。

set -uo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MSYS=/c/msys64
export PATH="/c/Users/sjw/.cargo/bin:$MSYS/mingw64/bin:/c/Program Files/Go/bin:$PATH"
export PKG_CONFIG_PATH="$MSYS/mingw64/lib/pkgconfig"
export GOPROXY="https://goproxy.cn,direct"      # 国内直连 proxy.golang.org 常超时

WHICH="${1:-all}"
rc=0

build_codex() {
    echo "=== [1/2] codex-asr（Rust → gnu 工具链）==="
    cd "$PROJ/vendor/codex-asr" || return 1

    # 坑 1：**不能用** `cargo build --target x86_64-pc-windows-gnu`。
    # rustup 装的是「工具链」不是给 msvc 工具链装「target」，那样写会报
    #   error[E0463]: can't find crate for `core`
    # 长得像 rlib 不兼容，其实只是 std 没装。正解是切工具链、不带 --target。
    #
    # 坑 2：`--no-default-features` 是必须的。上游默认 feature 是
    #   ["server","streaming"]，而 Voice Pill 只要 streaming；不关掉 server
    # 会多编一堆 HTTP 依赖。
    cargo +stable-x86_64-pc-windows-gnu build --release --locked \
        --no-default-features --features streaming \
        --target-dir "$PROJ/build" || return 1

    cp "$PROJ/build/release/codex-asr.exe" "$PROJ/bin/codex-asr.exe" || return 1
    echo "    → bin/codex-asr.exe"
}

build_freeasr() {
    echo "=== [2/2] freeasr（Go + CGO + libopus）==="
    cd "$PROJ/vendor/FreeASR" || return 1

    # 坑 3：`CGO_LDFLAGS` 里的 `-logg` 不能省。
    # gopkg.in/hraban/opus.v2 的 cgo 指令是 `pkg-config: opus opusfile`，
    # 而静态链接 libopusfile.a 需要 libogg 的 `ogg_*` 符号，pkg-config 没带出来。
    # 少了它报一屏 `undefined reference to ogg_sync_reset`。
    #
    # 坑 4：不加 `-static` 的话，产物在运行期要 libopus-0.dll / libopusfile-0.dll，
    # 报 `error while loading shared libraries`。加上就出单文件。
    CGO_ENABLED=1 CC=x86_64-w64-mingw32-gcc \
    CGO_LDFLAGS="-L$MSYS/mingw64/lib -lopusfile -lopus -logg" \
        go build -ldflags "-linkmode external -extldflags -static" \
        -o "$PROJ/bin/freeasr.exe" . || return 1

    echo "    → bin/freeasr.exe"
}

case "$WHICH" in
    codex)  build_codex  || rc=1 ;;
    freeasr) build_freeasr || rc=1 ;;
    all)    build_codex  || rc=1
            build_freeasr || rc=1 ;;
    *)      echo "用法: $0 [codex|freeasr|all]" >&2; exit 2 ;;
esac

echo
if [ "$rc" -eq 0 ]; then
    echo "=== 编译完成 ==="
    ls -la "$PROJ/bin/"*.exe 2>/dev/null
else
    echo "=== 有失败，见上 ===" >&2
fi
exit "$rc"
