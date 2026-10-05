// Package sentinel 是「Codex 一露头就把引擎拉起来」的哨兵。
//
// 为什么需要它
// ------------
// 引擎唯一的拉起点本来是 SessionStart 钩子，而钩子要等一次会话真的开始才打。
// 实测（2026-10-05 晚）：桌面壳 21:58:35 起来，钩子 21:59:30 才打——中间 55
// 秒按 Fn 没有任何反应，再加上拉起后约 4 秒预热。用户要的是「开 Codex 十秒内
// （±5 秒）按 Fn 就有人应答」，所以在钩子之外补一个更早的拉起点：2 秒扫一次
// 进程表，认出 Codex 就写锚点、拉看门狗，总共 6 秒上下。
//
// 它和钩子是什么关系
// ------------------
// 两者做的是同一件事（写纸条 + 幂等拉起看门狗），只是时机不同。谁先到都不冲突：
//   - 纸条写的是同一个「最外层 Codex」pid，后到的那个看到 pid 没变，一个字节
//     都不动（engine.WriteNote 的既有行为）。
//   - 看门狗是命名互斥体保护的，重复拉只会得到 supervisor_already_running。
//
// 为什么写纸条而不是直接把 pid 传给看门狗
// --------------------------------------
// 看门狗认的是纸条（src/anchor.py）。哨兵不是 Codex 的子孙，父链里没有它，
// 所以它不能像钩子那样「往上走」，只能先扫出候选进程，再用与钩子同一套判据
// （engine.FindAnchorFrom）认出最外层那个，然后替钩子把同一张纸条写上。
//
// 纪律：不跟「明确停过」对着干
// ---------------------------
// 纸条挡在拉起之前：**纸条上已经写着这个 pid 就不动**。所以用户跑过
// `main.py --stop` 之后（纸条还在、Codex 还开着），哨兵不会偷偷把引擎再拉起
// ——这与看门狗「退出码 0 就一起收摊」是同一条纪律。代价是「看门狗自己崩了
// 而 Codex 还开着」这一种情形哨兵不管；下一次会话开始时钩子照旧兜底。
package sentinel

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"

	"voicepill/internal/bridge"
	"voicepill/internal/engine"
)

const (
	// PollInterval 是「Codex 露头没有」的扫描间隔。2 秒 + 预热 4 秒 ≈ 6 秒，
	// 落在用户要的 10±5 秒里。
	PollInterval = 2 * time.Second
	// GonePoll 是「应用还在不在」的间隔：已经在跑了，不必那么勤。
	GonePoll = time.Second

	// RunKeyPath / RunValueName 是登录自启那一行的位置：HKCU，用户级，不需要管理员。
	RunKeyPath   = `Software\Microsoft\Windows\CurrentVersion\Run`
	RunValueName = "VoicePillSentinel"
	// KeyPathEnv 只给自测换一个键路径用（换到自己的测试子键里，绝不碰真的 Run）。
	KeyPathEnv = "VOICEPILL_SENTINEL_RUN_KEY"

	// SentinelMutexName 保证同时只有一个哨兵；默认名不许改。
	SentinelMutexName = `Local\VoicePill.Windows.Sentinel`
	// SentinelMutexEnv 只给自测换个名字用。
	SentinelMutexEnv = "VOICEPILL_SENTINEL_MUTEX"

	// Kind 是 exe 的子命令名，与 main.go 的分发一一对应。
	Kind = "sentinel"

	// FakeAppEnv 只给自测喂假进程表用（`pid:image,pid:image`），不设时扫真系统。
	FakeAppEnv = "VOICEPILL_SENTINEL_FAKE_APP"
)

// Watcher 是哨兵本体。几个口都是函数，为的是让单测把它们换成假的——
// 自测绝不许碰真进程、真看门狗、真注册表。
type Watcher struct {
	Log   engine.Logger
	Poll  time.Duration
	Every time.Duration

	// Scan 返回「映像名像 Codex」的进程号。
	Scan func() []int
	// Anchor 从一个进程号出发认最外层 Codex，返回 (pid, image, ok)。
	Anchor func(pid int) (int, string, bool)
	// Note 读锚点纸条。
	Note func() (engine.Note, bool)
	// Bind 写纸条 + 幂等拉起看门狗。
	Bind func(pid int, image string) bool
	// Alive 问「这个 Codex 还在不在」。
	Alive func(pid int) bool
	// Stop 只给单测收尾用：nil = 永远跑（生产就是这样，靠进程退出收场）。
	Stop <-chan struct{}

	// last 是上一轮认下来的窗口 pid：只在「刚认下来」时记一条日志，
	// 不然每 2 秒一行会把 plugin.log 刷满。
	last int
}

// New 造一个真哨兵。VOICEPILL_SENTINEL_FAKE_APP 非空时换成假进程表。
func New() *Watcher {
	w := &Watcher{
		Log:    engine.PluginLog,
		Poll:   PollInterval,
		Every:  GonePoll,
		Scan:   CodexPids,
		Anchor: engine.FindAnchorFrom,
		Note:   engine.ReadNote,
		Alive:  ProcessAlive,
	}
	w.Bind = w.bind
	if apps, ok := fakeApps(); ok {
		// 自测：进程表是假的，认锚点也照假表认——不许去走真的父链。
		w.Scan = func() []int {
			pids := make([]int, 0, len(apps))
			for _, app := range apps {
				pids = append(pids, app.pid)
			}
			return pids
		}
		w.Anchor = func(pid int) (int, string, bool) {
			for _, app := range apps {
				if app.pid == pid {
					return app.pid, app.image, true
				}
			}
			return 0, "", false
		}
		// 假世界里没有「断气」这一档：表里写着就是活的。混用真判活（ProcessAlive）
		// 会把每一个假 pid 都判死——那不是被测的行为，是尺子自己搭错了。
		w.Alive = func(pid int) bool {
			for _, app := range apps {
				if app.pid == pid {
					return true
				}
			}
			return false
		}
	}
	if w.Poll <= 0 {
		w.Poll = PollInterval
	}
	if w.Every <= 0 {
		w.Every = GonePoll
	}
	return w
}

func emit(log engine.Logger, event string, fields ...any) {
	if log == nil {
		engine.PluginLog(event, fields...)
		return
	}
	log(event, fields...)
}

type app struct {
	pid   int
	image string
}

// fakeApps 解析 VOICEPILL_SENTINEL_FAKE_APP。设了变量就返回 ok=true（哪怕是空的
// 列表）——自测要的是「这一趟确定没有 Codex」，不能让空值退回真扫描。
func fakeApps() ([]app, bool) {
	raw, set := os.LookupEnv(FakeAppEnv)
	if !set || strings.TrimSpace(raw) == "" {
		if set {
			return []app{}, true
		}
		return nil, false
	}
	apps := []app{}
	for _, item := range strings.Split(raw, ",") {
		parts := strings.SplitN(strings.TrimSpace(item), ":", 2)
		if len(parts) != 2 {
			continue
		}
		pid, err := strconv.Atoi(strings.TrimSpace(parts[0]))
		if err != nil {
			continue
		}
		image := strings.TrimSpace(parts[1])
		// 与真扫描同一把尺子：名字不像 Codex 的一律不算。
		if !engine.IsCodexImage(image) {
			continue
		}
		apps = append(apps, app{pid: pid, image: image})
	}
	return apps, true
}

// bind 写纸条（认准 pid）+ 幂等拉起看门狗。纸条写不上就不拉：看门狗靠纸条
// 认人，拉一个没人认领的引擎只会让它常驻到天荒地老。
func (w *Watcher) bind(pid int, image string) bool {
	if !engine.WriteNote(pid, image) {
		emit(w.Log, "sentinel_note_failed", "pid", pid)
		return false
	}
	emit(w.Log, "sentinel_bound", "pid", pid, "image", filepath.Base(image))
	engine.EnsureSupervisor(engine.Root(w.Log), w.Log)
	return true
}

// Tick 走一轮：返回「这一轮认下来的 Codex pid」（0 = 现在没有）。
func (w *Watcher) Tick() (pid int, bound bool) {
	found, image, ok := w.find()
	if !ok {
		return 0, false
	}
	// 进程表里可能还挂着「刚断气」的号（正在退出的那一瞬）。认下来之前先判活：
	// 给一个已经死掉的进程写锚点，看门狗会立刻收摊，等于白拉一次。
	if !w.Alive(found) {
		return 0, false
	}
	if note, has := w.Note(); has && note.PID == found {
		if w.last != found {
			emit(w.Log, "sentinel_note_kept", "pid", found)
		}
		w.last = found
		return found, false
	}
	emit(w.Log, "sentinel_app", "pid", found, "image", filepath.Base(image))
	w.last = found
	if !w.Bind(found, image) {
		return found, false
	}
	return found, true
}

// find 扫一遍候选进程，认出最外层那个 Codex。
func (w *Watcher) find() (int, string, bool) {
	bestPID, bestImage := 0, ""
	seen := map[int]bool{}
	for _, candidate := range w.Scan() {
		pid, image, ok := w.Anchor(candidate)
		if !ok || seen[pid] {
			continue
		}
		seen[pid] = true
		if bestPID == 0 || preferImage(image, bestImage) {
			bestPID, bestImage = pid, image
		}
	}
	if bestPID == 0 {
		return 0, "", false
	}
	return bestPID, bestImage, true
}

// preferImage：chatgpt.exe（桌面壳）比 codex.exe（会话里的运行时）长寿——
// 会话一换，里面那个 codex.exe 就没了，盯它会把锚点绑在一个短命进程上。
func preferImage(image, current string) bool {
	isShell := strings.EqualFold(filepath.Base(image), "chatgpt.exe")
	hasShell := strings.EqualFold(filepath.Base(current), "chatgpt.exe")
	return isShell && !hasShell
}

// Watch 是常驻主循环：认下来 → 盯到它退出 → 回到等待。
func (w *Watcher) Watch() {
	emit(w.Log, "sentinel_start", "poll_seconds", w.Poll.Seconds())
	for {
		pid, _ := w.Tick()
		if pid == 0 {
			if !w.wait(w.Poll) {
				return
			}
			continue
		}
		w.watchApp(pid)
		if !w.wait(w.Poll) {
			return
		}
	}
}

// watchApp 盯着一个 Codex 直到它走。
func (w *Watcher) watchApp(pid int) {
	for w.Alive(pid) {
		if !w.wait(w.Every) {
			return
		}
	}
	emit(w.Log, "sentinel_app_gone", "pid", pid)
	w.last = 0
}

// wait 睡一会儿；Stop 被关掉就返回 false（只给单测用；生产里 Stop 是 nil）。
func (w *Watcher) wait(d time.Duration) bool {
	if w.Stop == nil {
		time.Sleep(d)
		return true
	}
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-timer.C:
		return true
	case <-w.Stop:
		return false
	}
}

// CodexPids 扫全系统的进程表，返回映像名认得出是 Codex 的进程号。
func CodexPids() []int {
	snapshot, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return nil
	}
	defer windows.CloseHandle(snapshot)
	var entry windows.ProcessEntry32
	entry.Size = uint32(unsafe.Sizeof(entry))
	var pids []int
	for err := windows.Process32First(snapshot, &entry); err == nil; err = windows.Process32Next(snapshot, &entry) {
		if engine.IsCodexImage(windows.UTF16ToString(entry.ExeFile[:])) {
			pids = append(pids, int(entry.ProcessID))
		}
	}
	return pids
}

// processAlive 只问「这个号还活着吗」——不核映像，哨兵自己那张纸条要用它。
func processAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	handle, err := windows.OpenProcess(windows.SYNCHRONIZE|windows.PROCESS_QUERY_LIMITED_INFORMATION,
		false, uint32(pid))
	if err != nil {
		return false
	}
	defer windows.CloseHandle(handle)
	event, err := windows.WaitForSingleObject(handle, 0)
	if err != nil {
		return false
	}
	// WAIT_TIMEOUT = 还没信号 = 还活着；WAIT_OBJECT_0 = 进程已经没了。
	return event == uint32(windows.WAIT_TIMEOUT)
}

// ProcessAlive 问「这个号还是不是那个 Codex」：除了判活，再核一次映像。
// PID 会被系统回收，只认号迟早认错人。
func ProcessAlive(pid int) bool {
	return processAlive(pid) && engine.IsCodexImage(engine.ProcessImage(pid))
}

// ---------- 哨兵自己的纸条（--status / --stop 认它）----------

// Record 是哨兵留给命令行看的纸条：我还在、我是谁。
type Record struct {
	PID int     `json:"pid"`
	Exe string  `json:"exe"`
	At  float64 `json:"at"`
}

// RecordPath 是哨兵纸条的路径（与锚点纸条同一个目录）。
func RecordPath() string { return filepath.Join(bridge.AppDir(), "sentinel.json") }

// ReadRecord 读哨兵纸条。读不到 / 格式不对 / pid 非法 → ok=false。
func ReadRecord() (Record, bool) {
	raw, err := os.ReadFile(RecordPath())
	if err != nil {
		return Record{}, false
	}
	var rec Record
	if err := json.Unmarshal(raw, &rec); err != nil {
		return Record{}, false
	}
	if rec.PID <= 0 {
		return Record{}, false
	}
	return rec, true
}

// resolveExePath 把 exe 归一到真实路径再记纸条：插件是用目录联接装的，
// os.Executable() 给的是联接那一边，而 ProcessImage() 给的是联接背后的真实位置。
// 不归一就会「明明在跑却说没在跑」（实测），连 --stop 都会拒绝动手。自测换掉它。
var resolveExePath = engine.FinalPath

// recordSelf 写下「我是谁、我在哪儿」——先把路径归一，再落盘。
func recordSelf() bool {
	exe, err := exePath()
	if err != nil {
		return false
	}
	return writeRecord(resolveExePath(exe))
}

func writeRecord(exe string) bool {
	path := RecordPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o777); err != nil {
		return false
	}
	raw, err := json.Marshal(Record{PID: os.Getpid(), Exe: exe, At: float64(time.Now().UnixNano()) / 1e9})
	if err != nil {
		return false
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o644); err != nil {
		return false
	}
	if err := os.Rename(tmp, path); err != nil {
		os.Remove(tmp)
		return false
	}
	return true
}

func removeRecord() { _ = os.Remove(RecordPath()) }

// SentinelRunning 问「哨兵在不在跑」。认人不认号：纸条上的 pid 要活着，
// 而且映像路径得与纸条一致（号被复用就当没有）。
func SentinelRunning() (bool, int) {
	rec, ok := ReadRecord()
	if !ok {
		return false, 0
	}
	if !processAlive(rec.PID) {
		return false, 0
	}
	if rec.Exe != "" && !samePath(engine.ProcessImage(rec.PID), rec.Exe) {
		return false, 0
	}
	return true, rec.PID
}

func samePath(a, b string) bool {
	return a != "" && b != "" && strings.EqualFold(filepath.Clean(a), filepath.Clean(b))
}

// ---------- 登录自启登记（HKCU\...\Run）----------

// RunStore 是「登录自启」那一行的读写口。默认走 HKCU；自测换成自己的子键
// （或假实现），绝不碰真的 Run 键。
type RunStore interface {
	Path() string
	Get(name string) (string, bool, error)
	Set(name, value string) error
	Delete(name string) error
}

type hkcuStore struct{ path string }

func (s hkcuStore) Path() string { return s.path }

func (s hkcuStore) Get(name string) (string, bool, error) {
	key, err := registry.OpenKey(registry.CURRENT_USER, s.path, registry.QUERY_VALUE)
	if err != nil {
		if errors.Is(err, registry.ErrNotExist) {
			return "", false, nil
		}
		return "", false, err
	}
	defer key.Close()
	value, _, err := key.GetStringValue(name)
	if err != nil {
		if errors.Is(err, registry.ErrNotExist) {
			return "", false, nil
		}
		return "", false, err
	}
	return value, true, nil
}

func (s hkcuStore) Set(name, value string) error {
	key, _, err := registry.CreateKey(registry.CURRENT_USER, s.path, registry.SET_VALUE)
	if err != nil {
		return err
	}
	defer key.Close()
	return key.SetStringValue(name, value)
}

func (s hkcuStore) Delete(name string) error {
	key, err := registry.OpenKey(registry.CURRENT_USER, s.path, registry.SET_VALUE)
	if err != nil {
		if errors.Is(err, registry.ErrNotExist) {
			return nil
		}
		return err
	}
	defer key.Close()
	if err := key.DeleteValue(name); err != nil && !errors.Is(err, registry.ErrNotExist) {
		return err
	}
	return nil
}

// StorePath 取当前要用的键路径（自测可用 KeyPathEnv 换到自己的子键）。
func StorePath() string {
	if path := strings.TrimSpace(os.Getenv(KeyPathEnv)); path != "" {
		return path
	}
	return RunKeyPath
}

// DefaultStore 是真实现（HKCU）。
func DefaultStore() RunStore { return hkcuStore{path: StorePath()} }

// Command 是登记进 Run 的那一行：带引号的 exe + sentinel 子命令。
// 引号不能省：仓库路径里有空格或者 `&` 都会把它劈开。
func Command(exe string) string { return `"` + exe + `" sentinel` }

// Install 写登录自启那一行。返回进程退出码（0 成功）。
func Install(store RunStore, exe string, out io.Writer) int {
	line := Command(exe)
	if err := store.Set(RunValueName, line); err != nil {
		engine.PluginLog("sentinel_install_failed", "error", err)
		fmt.Fprintf(out, "登记失败：%v\n", err)
		return 1
	}
	engine.PluginLog("sentinel_install", "key", store.Path(), "value", line)
	fmt.Fprintf(out, "已登记登录自启：HKCU\\%s\n  %s = %s\n", store.Path(), RunValueName, line)
	fmt.Fprintln(out, "卸载：voicepill.exe sentinel --uninstall")
	return 0
}

// Uninstall 删掉登录自启那一行（没登记过也算成功——它要的结果已经在了）。
func Uninstall(store RunStore, out io.Writer) int {
	if err := store.Delete(RunValueName); err != nil {
		engine.PluginLog("sentinel_uninstall_failed", "error", err)
		fmt.Fprintf(out, "卸载失败：%v\n", err)
		return 1
	}
	engine.PluginLog("sentinel_uninstall", "key", store.Path())
	fmt.Fprintf(out, "已删掉登录自启：HKCU\\%s\\%s\n", store.Path(), RunValueName)
	return 0
}

func exePath() (string, error) {
	exe, err := os.Executable()
	if err != nil {
		return "", err
	}
	for _, prefix := range []string{`\\?\UNC\`, `\\?\`, `\??\`} {
		if strings.HasPrefix(exe, prefix) {
			exe = strings.TrimPrefix(exe, prefix)
			break
		}
	}
	return exe, nil
}

// ---------- 命令行 ----------

// Run 是 main.go 的分发入口；args 是 `sentinel` 后面的参数。
func Run(args []string, out io.Writer) int {
	_ = windows.SetConsoleOutputCP(65001) // 中文别在控制台里变成乱码
	if len(args) > 0 {
		switch args[0] {
		case "--install":
			exe, err := exePath()
			if err != nil {
				fmt.Fprintf(out, "拿不到自己的路径：%v\n", err)
				return 1
			}
			return Install(DefaultStore(), exe, out)
		case "--uninstall":
			return Uninstall(DefaultStore(), out)
		case "--status":
			return status(out)
		case "--once":
			return once(New(), out)
		case "--stop":
			return stopSentinel(out)
		default:
			fmt.Fprintf(out, "voicepill sentinel：不认识的参数 %q\n", args[0])
			return 2
		}
	}
	return runForever(New(), out)
}

// runForever 常驻：抢到单实例互斥体才继续，然后写纸条、进主循环。
func runForever(w *Watcher, out io.Writer) int {
	name, err := windows.UTF16PtrFromString(mutexName())
	if err != nil {
		fmt.Fprintf(out, "哨兵起不来：%v\n", err)
		return 1
	}
	handle, err := windows.CreateMutex(nil, true, name)
	if err != nil {
		if errors.Is(err, windows.ERROR_ALREADY_EXISTS) {
			if handle != 0 {
				windows.CloseHandle(handle)
			}
			fmt.Fprintln(out, "已经有一个哨兵在跑了，这次退出。")
			return 0
		}
		fmt.Fprintf(out, "哨兵起不来：%v\n", err)
		return 1
	}
	defer windows.ReleaseMutex(handle)
	defer windows.CloseHandle(handle)

	if recordSelf() {
		defer removeRecord()
	}
	fmt.Fprintf(out, "哨兵起来了：每 %.1f 秒扫一次 Codex。日志：%s\n", w.Poll.Seconds(), engine.LogPath())
	w.Watch()
	return 0
}

func mutexName() string {
	if name := strings.TrimSpace(os.Getenv(SentinelMutexEnv)); name != "" {
		return name
	}
	return SentinelMutexName
}

func once(w *Watcher, out io.Writer) int {
	pid, bound := w.Tick()
	switch {
	case pid == 0:
		fmt.Fprintln(out, "没找到 Codex（chatgpt.exe / codex.exe）。")
	case bound:
		fmt.Fprintf(out, "认下 Codex pid=%d：锚点写好了，看门狗也拉了。\n", pid)
	default:
		fmt.Fprintf(out, "Codex pid=%d 的锚点已经在了，不动。\n", pid)
	}
	return 0
}

func status(out io.Writer) int {
	store := DefaultStore()
	switch value, ok, err := store.Get(RunValueName); {
	case err != nil:
		fmt.Fprintf(out, "登录自启：读不出来（%v）\n", err)
	case ok:
		fmt.Fprintf(out, "登录自启：已登记 → %s\n", value)
	default:
		fmt.Fprintln(out, "登录自启：没登记（要常驻就跑 voicepill.exe sentinel --install）")
	}
	if running, pid := SentinelRunning(); running {
		fmt.Fprintf(out, "哨兵    ：在跑（pid=%d）\n", pid)
	} else {
		fmt.Fprintln(out, "哨兵    ：没在跑")
	}
	if note, ok := engine.ReadNote(); ok {
		live := "已经不在"
		if ProcessAlive(note.PID) {
			live = "在跑"
		}
		fmt.Fprintf(out, "锚点    ：pid=%d %s（%s）\n", note.PID, filepath.Base(note.Image), live)
	} else {
		fmt.Fprintln(out, "锚点    ：没有纸条")
	}
	return 0
}

func stopSentinel(out io.Writer) int {
	rec, ok := ReadRecord()
	if !ok {
		fmt.Fprintln(out, "没有哨兵纸条（本来就没跑，或者纸条被清掉了）。")
		return 1
	}
	handle, err := windows.OpenProcess(
		windows.PROCESS_TERMINATE|windows.SYNCHRONIZE|windows.PROCESS_QUERY_LIMITED_INFORMATION,
		false, uint32(rec.PID))
	if err != nil {
		fmt.Fprintf(out, "哨兵（pid=%d）已经不在跑了。\n", rec.PID)
		removeRecord()
		return 1
	}
	defer windows.CloseHandle(handle)
	// 认人不认号：先核映像，再动手。号码被复用时不至于误伤别人。
	if !samePath(engine.ProcessImage(rec.PID), rec.Exe) {
		fmt.Fprintf(out, "pid=%d 现在不是 %s，不动它。\n", rec.PID, rec.Exe)
		removeRecord()
		return 1
	}
	if err := windows.TerminateProcess(handle, 0); err != nil {
		fmt.Fprintf(out, "请哨兵退出失败：%v\n", err)
		return 1
	}
	removeRecord()
	fmt.Fprintf(out, "已请哨兵（pid=%d）退出。引擎不受影响，仍由看门狗管。\n", rec.PID)
	return 0
}
