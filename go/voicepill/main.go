// voicepill 是 Voice Pill 的插件侧单文件可执行程序。
//
// 不带子命令时按 MCP stdio 服务端跑（八个工具）；带子命令时按 Codex 的命令行
// 钩子跑（session-start / prompt-submit / stop，见 internal/hooks）。
package main

import (
	"fmt"
	"os"

	"voicepill/internal/bridge"
	"voicepill/internal/hooks"
	"voicepill/internal/tools"
)

func main() {
	if len(os.Args) > 1 {
		switch os.Args[1] {
		case hooks.KindSessionStart, hooks.KindPromptSubmit, hooks.KindStop:
			os.Exit(hooks.Run(os.Args[1], os.Stdin, os.Stdout))
		default:
			fmt.Fprintf(os.Stderr, "voicepill: 不认识的子命令 %q\n", os.Args[1])
			os.Exit(2)
		}
	}
	if err := tools.Serve(bridge.NewClient()); err != nil {
		fmt.Fprintln(os.Stderr, err) // stdout 是协议通道，只能往 stderr 说话
		os.Exit(1)
	}
}
