package engine

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/windows"
)

// capture 收日志，便于断言「走了哪一级 / 命中了哪条分支」。
type capture struct{ lines []string }

func (c *capture) log(event string, fields ...any) {
	var b strings.Builder
	b.WriteString(event)
	for i := 0; i+1 < len(fields); i += 2 {
		fmt.Fprintf(&b, " %v=%v", fields[i], fields[i+1])
	}
	c.lines = append(c.lines, b.String())
}

func (c *capture) has(sub string) bool {
	for _, line := range c.lines {
		if strings.Contains(line, sub) {
			return true
		}
	}
	return false
}

func uniqueMutexName(t *testing.T) string {
	t.Helper()
	return fmt.Sprintf(`Local\VoicePillTest-%d-%d`, os.Getpid(), time.Now().UnixNano())
}

// ---------- 引擎根：三级解析 ----------

func TestRootEnvWins(t *testing.T) {
	dir := t.TempDir()
	t.Setenv(EngineRootEnv, dir)
	c := &capture{}
	if got := Root(c.log); got != dir {
		t.Fatalf("Root()=%q，想要 %q", got, dir)
	}
	if !c.has("source=env") {
		t.Fatalf("没记「来自环境变量」：%v", c.lines)
	}
}

func TestResolveRootOrder(t *testing.T) {
	c := &capture{}
	selfCalls, jsonCalls := 0, 0
	self := func(root string, err error) func() (string, error) {
		return func() (string, error) { selfCalls++; return root, err }
	}
	fromJSON := func(root string) func() string {
		return func() string { jsonCalls++; return root }
	}

	if got := resolveRoot(c.log, "E", self("S", nil), fromJSON("J")); got != "E" {
		t.Fatalf("env 应当最高优先，得到 %q", got)
	}
	if selfCalls != 0 || jsonCalls != 0 {
		t.Fatalf("env 命中后不该再走别的：self=%d json=%d", selfCalls, jsonCalls)
	}

	if got := resolveRoot(c.log, "", self("S", nil), fromJSON("J")); got != "S" {
		t.Fatalf("自解析应当压过 engine.json，得到 %q", got)
	}
	if jsonCalls != 0 {
		t.Fatalf("自解析命中后不该读 engine.json")
	}

	if got := resolveRoot(c.log, "", self("", errors.New("boom")), fromJSON("J")); got != "J" {
		t.Fatalf("自解析失败应当退 engine.json，得到 %q", got)
	}
	if got := resolveRoot(c.log, "", self("", nil), fromJSON("J")); got != "J" {
		t.Fatalf("自解析认不出根（空串）应当退 engine.json，得到 %q", got)
	}
	if !c.has("engine_root_self_missing") {
		t.Fatalf("自解析认不出根应当记 engine_root_self_missing：%v", c.lines)
	}
	if got := resolveRoot(c.log, "", self("", errors.New("boom")), fromJSON("")); got != "" {
		t.Fatalf("三级都拿不到应当回空串，得到 %q", got)
	}
	if !c.has("engine_root_missing") {
		t.Fatalf("缺 engine_root_missing 日志：%v", c.lines)
	}
}

func TestRootFromExeDir(t *testing.T) {
	repo := t.TempDir()
	if err := os.MkdirAll(filepath.Join(repo, "src"), 0o777); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"main.py", "supervise.py"} {
		if err := os.WriteFile(filepath.Join(repo, "src", name), []byte("#"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	cases := []struct{ name, dir, want string }{
		{"插件那份：<根>\\plugins\\voice-pill 往上两级", filepath.Join(repo, "plugins", "voice-pill"), repo},
		{"开发那份：<根>\\bin 往上一级", filepath.Join(repo, "bin"), repo},
		{"仓库根自己摆一份", repo, repo},
		{"摆在外面认不出：回空串，让 engine.json 接手", t.TempDir(), ""},
	}
	for _, tc := range cases {
		if got := rootFromExeDir(tc.dir); got != tc.want {
			t.Fatalf("%s：rootFromExeDir(%q)=%q，想要 %q", tc.name, tc.dir, got, tc.want)
		}
	}
}

func TestStripLongPrefix(t *testing.T) {
	cases := []struct{ in, want string }{
		{`\\?\D:\x\y`, `D:\x\y`},
		{`\\?\UNC\server\share\x`, `\\server\share\x`},
		{`\??\C:\x`, `C:\x`},
		{`D:\x`, `D:\x`},
	}
	for _, tc := range cases {
		if got := stripLongPrefix(tc.in); got != tc.want {
			t.Fatalf("stripLongPrefix(%q)=%q，想要 %q", tc.in, got, tc.want)
		}
	}
}

func TestFinalPathNameStripsPrefix(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "probe.txt")
	if err := os.WriteFile(file, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := finalPathName(file)
	if err != nil {
		t.Fatalf("finalPathName: %v", err)
	}
	if strings.HasPrefix(got, `\\?\`) || strings.HasPrefix(got, `\??\`) {
		t.Fatalf("没剥掉长路径前缀：%q", got)
	}
	if !strings.EqualFold(filepath.Base(got), "probe.txt") {
		t.Fatalf("文件名不对：%q", got)
	}
	if !strings.EqualFold(filepath.Dir(got), filepath.Dir(file)) {
		t.Fatalf("目录不对：%q vs %q", filepath.Dir(got), filepath.Dir(file))
	}
}

func TestJSONRoot(t *testing.T) {
	tmp := t.TempDir()
	t.Setenv("LOCALAPPDATA", tmp)
	if got := jsonRoot(); got != "" {
		t.Fatalf("没有 engine.json 时应当回空，得到 %q", got)
	}
	app := filepath.Join(tmp, "VoicePill")
	if err := os.MkdirAll(app, 0o777); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(app, "engine.json"), []byte(`{"root": "X:\\repo"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := jsonRoot(); got != `X:\repo` {
		t.Fatalf("jsonRoot=%q", got)
	}
	if err := os.WriteFile(filepath.Join(app, "engine.json"), []byte("not json"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := jsonRoot(); got != "" {
		t.Fatalf("坏 JSON 应当回空，得到 %q", got)
	}
}

// ---------- 看门狗：互斥体 ----------

// holdSupervisorMutex 在测试进程里按住互斥体，模拟「已经有一只守着了」。
func holdSupervisorMutex(t *testing.T, name string) {
	t.Helper()
	namePtr, err := windows.UTF16PtrFromString(name)
	if err != nil {
		t.Fatal(err)
	}
	handle, err := windows.CreateMutex(nil, true, namePtr)
	if err != nil {
		t.Fatalf("CreateMutex(%s): %v", name, err)
	}
	t.Cleanup(func() {
		windows.ReleaseMutex(handle)
		windows.CloseHandle(handle)
	})
}

func TestProbeSupervisorMutexFreshThenHeld(t *testing.T) {
	name := uniqueMutexName(t)
	t.Setenv(SupervisorMutexEnv, name)

	owned, err := ProbeSupervisorMutex()
	if err != nil {
		t.Fatalf("空着的互斥体不该报错：%v", err)
	}
	if !owned {
		t.Fatal("没人守时应当判 owned")
	}
	if SupervisorAlive() {
		t.Fatal("没人守时不该判「在守」")
	}

	holdSupervisorMutex(t, name)
	owned, err = ProbeSupervisorMutex()
	if err != nil {
		t.Fatalf("已有互斥体不该报错：%v", err)
	}
	if owned {
		t.Fatal("有人在守时不该判 owned")
	}
	if !SupervisorAlive() {
		t.Fatal("有人在守时应当判「在守」")
	}
}

// withFakeSpawner 把「真拉起 pythonw」那一步换成假实现，返回调用次数与入参；
// 测试结束自动还原。测试绝不许真起进程。
func withFakeSpawner(t *testing.T) (*int, *[]string) {
	t.Helper()
	old := spawnSupervisor
	calls := 0
	args := []string{}
	spawnSupervisor = func(pythonw, supervise, root string) error {
		calls++
		args = append(args, pythonw, supervise, root)
		return nil
	}
	t.Cleanup(func() { spawnSupervisor = old })
	return &calls, &args
}

// writeSuperviseStub 在假根里放一个 src\supervise.py 桩，让「文件存在」这一关
// 过得去——只有这样测试才真能走到 spawn 那一步。桩内容不会被运行。
func writeSuperviseStub(t *testing.T, root string) {
	t.Helper()
	dir := filepath.Join(root, "src")
	if err := os.MkdirAll(dir, 0o777); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "supervise.py"), []byte("stub"), 0o644); err != nil {
		t.Fatal(err)
	}
}

// 假根里有 supervise.py 桩、互斥体已被占：幂等守卫必须生效——一个字节都不许
// 拉，并记 supervisor_already_running。
//
// Mendel 审任务 7 时指出：原来的用例用「没有 supervise.py 的假根」，无论
// 互斥体守卫是否生效都走 supervise_missing，于是把 `if !owned` 改成恒假也
// 全绿。这条用例正是为了钉住它。
func TestEnsureSupervisorHeldSkipsSpawn(t *testing.T) {
	name := uniqueMutexName(t)
	t.Setenv(SupervisorMutexEnv, name)
	holdSupervisorMutex(t, name)

	root := t.TempDir()
	writeSuperviseStub(t, root)

	calls, _ := withFakeSpawner(t)
	c := &capture{}
	if EnsureSupervisor(root, c.log) {
		t.Fatal("已经有人在守，不该报「这次是我们拉的」")
	}
	if *calls != 0 {
		t.Fatalf("已经有人在守，绝不许调 spawner，实际调了 %d 次", *calls)
	}
	if !c.has("supervisor_already_running") {
		t.Fatalf("应当记 supervisor_already_running：%v", c.lines)
	}
	if c.has("supervisor_spawned") {
		t.Fatalf("不该记 supervisor_spawned：%v", c.lines)
	}
}

// 反向：互斥体空着 + 假根里有 supervise.py 桩 → 必须亲手拉起恰好一次，
// 并把三个入参原样交给 spawner。
func TestEnsureSupervisorSpawnsWhenFree(t *testing.T) {
	name := uniqueMutexName(t)
	t.Setenv(SupervisorMutexEnv, name)

	root := t.TempDir()
	writeSuperviseStub(t, root)

	calls, args := withFakeSpawner(t)
	c := &capture{}
	if !EnsureSupervisor(root, c.log) {
		t.Fatalf("互斥体空着时应当亲手拉起：%v", c.lines)
	}
	if *calls != 1 {
		t.Fatalf("应当调 spawner 恰好一次，实际 %d 次", *calls)
	}
	wantPythonw := filepath.Join(root, ".venv", "Scripts", "pythonw.exe")
	wantSupervise := filepath.Join(root, "src", "supervise.py")
	if len(*args) != 3 || (*args)[0] != wantPythonw || (*args)[1] != wantSupervise || (*args)[2] != root {
		t.Fatalf("spawner 入参不对：%q（想要 %q %q %q）", *args, wantPythonw, wantSupervise, root)
	}
	if !c.has("supervisor_spawned") {
		t.Fatalf("应当记 supervisor_spawned：%v", c.lines)
	}
}

func TestEnsureSupervisorFakeRootDoesNotSpawn(t *testing.T) {
	name := uniqueMutexName(t)
	t.Setenv(SupervisorMutexEnv, name)
	calls, _ := withFakeSpawner(t)
	c := &capture{}
	root := t.TempDir() // 假根：没有 src\supervise.py
	if EnsureSupervisor(root, c.log) {
		t.Fatal("假根里没有 supervise.py，不该拉")
	}
	if *calls != 0 {
		t.Fatalf("少了 supervise.py 就不该调 spawner，实际调了 %d 次", *calls)
	}
	if !c.has("supervise_missing") {
		t.Fatalf("应当记 supervise_missing：%v", c.lines)
	}
	if SupervisorAlive() {
		t.Fatal("我们不该留下一个被占着的互斥体")
	}
}

// ---------- 等就绪 ----------

func TestWaitReadyFastPath(t *testing.T) {
	c := &capture{}
	prober := func(time.Duration) (json.RawMessage, error) { return json.RawMessage(`{}`), nil }
	if !WaitReady(prober, c.log) {
		t.Fatal("status 通了就该算就绪")
	}
}

func TestWaitReadyStopsWhenSupervisorGone(t *testing.T) {
	oldWait, oldPoll := readyWait, readyPoll
	readyWait, readyPoll = 5*time.Second, time.Millisecond
	t.Cleanup(func() { readyWait, readyPoll = oldWait, oldPoll })

	aliveCalls := 0
	alive := func() bool { aliveCalls++; return aliveCalls == 1 } // 先见它活着，然后就不在了
	prober := func(time.Duration) (json.RawMessage, error) { return nil, errors.New("没通") }
	c := &capture{}
	start := time.Now()
	if waitReady(prober, alive, c.log) {
		t.Fatal("看门狗不在了就该收手")
	}
	if elapsed := time.Since(start); elapsed > time.Second {
		t.Fatalf("应当提前收手，实际等了 %v", elapsed)
	}
	if !c.has("supervisor_gone") {
		t.Fatalf("应当记 supervisor_gone：%v", c.lines)
	}
}

func TestWaitReadyTimesOutWithoutSupervisor(t *testing.T) {
	oldWait, oldPoll := readyWait, readyPoll
	readyWait, readyPoll = 40*time.Millisecond, 5*time.Millisecond
	t.Cleanup(func() { readyWait, readyPoll = oldWait, oldPoll })

	prober := func(time.Duration) (json.RawMessage, error) { return nil, errors.New("没通") }
	c := &capture{}
	if waitReady(prober, func() bool { return false }, c.log) {
		t.Fatal("一直不通就不该算就绪")
	}
	if c.has("supervisor_gone") {
		t.Fatalf("没见过它活着，不该记 supervisor_gone：%v", c.lines)
	}
	if !c.has("ready_timeout") {
		t.Fatalf("应当记 ready_timeout：%v", c.lines)
	}
}

// ---------- 锚点：父链与纸条 ----------

func TestFindAnchorChain(t *testing.T) {
	parents := map[int]int{100: 90, 90: 80, 80: 70, 70: 0}
	images := map[int]string{
		90: `C:\x\codex.exe`,
		80: `C:\y\ChatGPT.exe`,
		70: `C:\Windows\explorer.exe`,
	}
	pid, image, ok := findAnchor(100, func(p int) int { return parents[p] }, func(p int) string { return images[p] })
	if !ok || pid != 80 || image != `C:\y\ChatGPT.exe` {
		t.Fatalf("最外层应当是 (80, ChatGPT.exe)，得到 (%d, %q, %v)", pid, image, ok)
	}

	if _, _, ok := findAnchor(100, func(p int) int { return parents[p] }, func(int) string { return `C:\Windows\explorer.exe` }); ok {
		t.Fatal("链里没有 Codex 就不该认")
	}
}

func TestFindAnchorCycleAndCap(t *testing.T) {
	// 非 Codex 的二环：不能死循环，也不该认。
	parents := map[int]int{1: 2, 2: 1}
	if _, _, ok := findAnchor(1, func(p int) int { return parents[p] }, func(int) string { return `C:\Windows\explorer.exe` }); ok {
		t.Fatal("二环链不该认")
	}
	// 超长链：最多走 MaxHops 层，最外层是第 MaxHops 个父进程。
	parent := func(p int) int { return p + 1 }
	image := func(int) string { return `C:\x\codex.exe` }
	pid, _, ok := findAnchor(1, parent, image)
	if !ok || pid != 1+MaxHops {
		t.Fatalf("应当停在第 %d 层，得到 pid=%d ok=%v", MaxHops, pid, ok)
	}
}

func TestProcessImageAndParentSelf(t *testing.T) {
	img := processImage(os.Getpid())
	if img == "" {
		t.Fatal("拿不到自己的映像路径")
	}
	if !strings.HasSuffix(strings.ToLower(img), ".exe") {
		t.Fatalf("映像路径不像 exe：%q", img)
	}
	if parent := processParent(os.Getpid()); parent <= 0 {
		t.Fatalf("拿不到自己的父 PID：%d", parent)
	}
}

func TestWriteNoteDedupAndRead(t *testing.T) {
	t.Setenv("LOCALAPPDATA", t.TempDir())
	if WriteNote(0, "x") {
		t.Fatal("pid<=0 不该写")
	}
	if !WriteNote(123, `C:\a\codex.exe`) {
		t.Fatal("写纸条失败")
	}
	note, ok := ReadNote()
	if !ok || note.PID != 123 || note.Image != `C:\a\codex.exe` || note.At <= 0 {
		t.Fatalf("读回来的纸条不对：%+v ok=%v", note, ok)
	}
	first, err := os.ReadFile(NotePath())
	if err != nil {
		t.Fatal(err)
	}
	time.Sleep(20 * time.Millisecond)
	if !WriteNote(123, `C:\a\codex.exe`) {
		t.Fatal("第二次写（内容没变）应当返回 true")
	}
	second, err := os.ReadFile(NotePath())
	if err != nil {
		t.Fatal(err)
	}
	if string(first) != string(second) {
		t.Fatalf("内容没变就不该动文件：\n%s\n%s", first, second)
	}
	if !WriteNote(124, `C:\a\codex.exe`) {
		t.Fatal("换了 pid 应当重写")
	}
	third, err := os.ReadFile(NotePath())
	if err != nil {
		t.Fatal(err)
	}
	if string(third) == string(first) {
		t.Fatal("换了 pid 之后内容应当变")
	}
}

func TestReadNoteMalformed(t *testing.T) {
	tmp := t.TempDir()
	t.Setenv("LOCALAPPDATA", tmp)
	app := filepath.Join(tmp, "VoicePill")
	if err := os.MkdirAll(app, 0o777); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(app, "anchor.json")
	for _, bad := range []string{"not json", `{"pid": 0}`, `{"pid": -3, "image": "x"}`, `[]`} {
		if err := os.WriteFile(path, []byte(bad), 0o644); err != nil {
			t.Fatal(err)
		}
		if _, ok := ReadNote(); ok {
			t.Fatalf("坏纸条 %q 不该读成功", bad)
		}
	}
}

// ---------- _ensure_engine ----------

func TestEnsureReturnsStateWithoutSpawning(t *testing.T) {
	c := &capture{}
	prober := func(time.Duration) (json.RawMessage, error) {
		return json.RawMessage(`{"pid": 4242, "phase": "idle"}`), nil
	}
	got, err := Ensure(prober, c.log)
	if err != nil {
		t.Fatalf("探到了就不该报错：%v", err)
	}
	if string(got) != `{"pid": 4242, "phase": "idle"}` {
		t.Fatalf("状态应当原样回来，得到 %s", got)
	}
}

func TestEnsureErrorsWhenLauncherMissing(t *testing.T) {
	t.Setenv(EngineRootEnv, filepath.Join(t.TempDir(), "no-such-root"))
	c := &capture{}
	prober := func(time.Duration) (json.RawMessage, error) { return nil, errors.New("连不上") }
	_, err := Ensure(prober, c.log)
	if err == nil {
		t.Fatal("没有启动器就该报错")
	}
	if !strings.Contains(err.Error(), "找不到它的启动器") {
		t.Fatalf("错误文本不对：%v", err)
	}
}

// TestLiveAnchor 只在 VOICEPILL_LIVE=1 时跑：把 Go 的 FindAnchor 与 Python
// anchor.find_anchor() 对拍，同一进程树里 (pid, image) 必须逐字一致。
// 需要在仓库根下跑，并把 VOICEPILL_ENGINE_ROOT 指向仓库根（好找到 .venv 的 python）。
func TestLiveAnchor(t *testing.T) {
	if os.Getenv("VOICEPILL_LIVE") != "1" {
		t.Skip("设 VOICEPILL_LIVE=1 才跑真机锚点对拍")
	}
	pid, image, ok := FindAnchor()
	if !ok {
		t.Fatalf("没找到锚点：得在 Codex 的进程树里跑")
	}
	t.Logf("Go: pid=%d image=%s", pid, image)

	root := Root(nil)
	python := filepath.Join(root, ".venv", "Scripts", "python.exe")
	if !isFile(python) {
		t.Fatalf("找不到 %s（把 VOICEPILL_ENGINE_ROOT 指向仓库根再跑）", python)
	}
	script := `import sys, json; sys.path.insert(0, "src"); import anchor; a = anchor.find_anchor(); print(json.dumps({"pid": a[0], "image": a[1]} if a else None))`
	cmd := exec.Command(python, "-c", script)
	cmd.Dir = root
	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("跑 Python 对拍失败：%v", err)
	}
	var pyAnchor *struct {
		PID   int    `json:"pid"`
		Image string `json:"image"`
	}
	if err := json.Unmarshal(bytes.TrimSpace(out), &pyAnchor); err != nil {
		t.Fatalf("Python 输出不是 JSON：%v（%q）", err, out)
	}
	if pyAnchor == nil {
		t.Fatalf("Python 没找到锚点")
	}
	if pyAnchor.PID != pid || !strings.EqualFold(pyAnchor.Image, image) {
		t.Fatalf("对拍不一致：Go=(%d, %s) Python=(%d, %s)", pid, image, pyAnchor.PID, pyAnchor.Image)
	}
	t.Logf("对拍一致：pid=%d image=%s", pid, image)
}

// ---------- 预热（宿主刚把插件拉起来那一刻）----------

func TestWarmUpAnchorThenEnsure(t *testing.T) {
	oldAnchor, oldRoot, oldEnsure := warmAnchor, warmRoot, warmEnsure
	defer func() { warmAnchor, warmRoot, warmEnsure = oldAnchor, oldRoot, oldEnsure }()
	var calls []string
	warmAnchor = func(Logger) (int, string, bool) { calls = append(calls, "anchor"); return 7, "chatgpt.exe", true }
	warmRoot = func(Logger) string { calls = append(calls, "root"); return "D:\\repo" }
	warmEnsure = func(root string, _ Logger) bool { calls = append(calls, "ensure:"+root); return true }

	WarmUp(nil)

	want := []string{"anchor", "root", "ensure:D:\\repo"}
	if len(calls) != len(want) {
		t.Fatalf("调用顺序不对：%v，想要 %v", calls, want)
	}
	for i := range want {
		if calls[i] != want[i] {
			t.Fatalf("第 %d 步是 %q，想要 %q（全部：%v）", i, calls[i], want[i], calls)
		}
	}
}

func TestWarmUpNoRootSkipsEnsure(t *testing.T) {
	oldAnchor, oldRoot, oldEnsure := warmAnchor, warmRoot, warmEnsure
	defer func() { warmAnchor, warmRoot, warmEnsure = oldAnchor, oldRoot, oldEnsure }()
	var calls []string
	warmAnchor = func(Logger) (int, string, bool) { calls = append(calls, "anchor"); return 0, "", false }
	warmRoot = func(Logger) string { calls = append(calls, "root"); return "" }
	warmEnsure = func(string, Logger) bool { calls = append(calls, "ensure"); return false }

	WarmUp(nil)

	if len(calls) != 2 || calls[0] != "anchor" || calls[1] != "root" {
		t.Fatalf("认不出根时不该去拉看门狗：%v", calls)
	}
}

// 真实现跑一遍：假根里没有 src 下的 supervise.py，只该记一条 supervise_missing，绝不拉东西。
func TestWarmUpWithFakeRootDoesNotSpawn(t *testing.T) {
	t.Setenv("LOCALAPPDATA", t.TempDir())
	t.Setenv(SupervisorMutexEnv, uniqueMutexName(t))
	t.Setenv(EngineRootEnv, t.TempDir())
	c := &capture{}

	WarmUp(c.log)

	if !c.has("engine_root source=env") {
		t.Fatalf("没记根从哪来：%v", c.lines)
	}
	if !c.has("supervise_missing") {
		t.Fatalf("假根里应当只剩 supervise_missing：%v", c.lines)
	}
	if c.has("supervisor_spawned") {
		t.Fatalf("假根里绝不该拉看门狗：%v", c.lines)
	}
}
