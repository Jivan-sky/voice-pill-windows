// voicepill 是 Voice Pill 的插件侧单文件可执行程序。
//
// 不带子命令时按 MCP stdio 服务端跑（八个工具）。三个钩子入口
// （session-start / prompt-submit / stop）还没做，见到子命令直接拒绝，
// 不做半成品。
package main

import (
	"fmt"
	"os"

	"voicepill/internal/bridge"
	"voicepill/internal/tools"
)

func main() {
	if len(os.Args) > 1 {
		fmt.Fprintf(os.Stderr, "voicepill: 不认识的子命令 %q（钩子入口还没做）\n", os.Args[1])
		os.Exit(2)
	}
	if err := tools.Serve(bridge.NewClient()); err != nil {
		fmt.Fprintf(os.Stderr, "voicepill: %v\n", err)
		os.Exit(1)
	}
}
