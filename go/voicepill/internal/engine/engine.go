// Package engine 负责「把驻留引擎拉起来，并把它的命绑在 Codex 上」。
//
// 引擎本体仍是 Python，一行不改；这里只管四件事：
//
//  1. 引擎根的三级解析（环境变量 → 自解析 → %LOCALAPPDATA%\VoicePill\engine.json）；
//  2. 幂等拉起看门狗（命名互斥体 Local\VoicePill.Windows.Supervisor）；
//  3. 等就绪（≤6 秒，每 0.3 秒探一次 status）；
//  4. 锚点纸条（父链最多 16 层，认最外层的 codex.exe / chatgpt.exe）。
//
// 逐条对齐 Python 侧：src/config.py::app_dir()、src/anchor.py、
// plugins/voice-pill/hook_session_start.py、
// plugins/voice-pill/mcp_server.py::_ensure_engine()。
package engine

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"

	"voicepill/internal/bridge"
)

const (
	// EngineRootEnv 是引擎根的最高优先级覆盖，自测用。
	EngineRootEnv = "VOICEPILL_ENGINE_ROOT"
	// SupervisorMutexName 与 Python 侧 single_instance.SUPERVISOR_MUTEX_NAME
	// 一字不差。默认值不许改。
	SupervisorMutexName = `Local\VoicePill.Windows.Supervisor`
	// SupervisorMutexEnv 只给自测换个名字用；不设时就是上面的默认名。
	SupervisorMutexEnv = "VOICEPILL_SUPERVISOR_MUTEX"
	// MaxHops 是父链最多往上走几层，对齐 Python anchor.MAX_HOPS。
	MaxHops = 16
	// ReadyWaitSeconds / ReadyPollSeconds 对齐 Python hook_session_start。
	ReadyWaitSeconds = 6.0
	ReadyPollSeconds = 0.3
)

// CodexImageNames 与 Python 侧 anchor.CODEX_IMAGE_NAMES 一致：按文件名比，
// 大小写不敏感。
var CodexImageNames = []string{"codex.exe", "chatgpt.exe"}

// 就绪等待与 _ensure_engine 的节奏；单测会把它们调小。
var (
	readyWait          = time.Duration(ReadyWaitSeconds * float64(time.Second))
	readyPoll          = time.Duration(ReadyPollSeconds * float64(time.Second))
	ensureProbeTimeout = 4 * time.Second
	ensurePollTimeout  = 2 * time.Second
	ensureWait         = 20 * time.Second
	ensurePollInterval = 500 * time.Millisecond
)

// Logger 记一条引擎日志，字段是 k/v 成对。nil 表示用 PluginLog。
type Logger func(event string, fields ...any)

func emit(log Logger, event string, fields ...any) {
	if log == nil {
		PluginLog(event, fields...)
		return
	}
	log(event, fields...)
}

// LogPath 是插件日志，与 Python 侧 hook_session_start.LOG_PATH 同一个文件。
func LogPath() string { return filepath.Join(bridge.AppDir(), "plugin.log") }

// PluginLog 把日志追加到 plugin.log，格式对齐 Python 侧 _log：
//
//	2026-10-03 18:52:23 pid=1234 hook_session_start session=... source=...
//
// 出错就静默——日志不该拖垮钩子。
func PluginLog(event string, fields ...any) {
	path := LogPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o777); err != nil {
		return
	}
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return
	}
	defer f.Close()
	var b strings.Builder
	b.WriteString(time.Now().Format("2006-01-02 15:04:05"))
	fmt.Fprintf(&b, " pid=%d %s", os.Getpid(), event)
	for i := 0; i+1 < len(fields); i += 2 {
		fmt.Fprintf(&b, " %v=%v", fields[i], fields[i+1])
	}
	b.WriteByte('\n')
	_, _ = f.WriteString(b.String())
}

// ---------- 引擎根：三级解析 ----------

// Root 三级解析引擎根，每级都记日志。三级都拿不到返回 ""：
// 这不是错误，只是「拉不起引擎」——status/take 该失败失败，钩子照常打完。
func Root(log Logger) string {
	return resolveRoot(log, strings.TrimSpace(os.Getenv(EngineRootEnv)), selfRoot, jsonRoot)
}

func resolveRoot(log Logger, envRoot string, self func() (string, error), fromJSON func() string) string {
	if envRoot != "" {
		emit(log, "engine_root", "source", "env", "root", envRoot)
		return envRoot
	}
	root, err := self()
	switch {
	case err != nil:
		emit(log, "engine_root_self_failed", "error", errText(err))
	case root == "":
		emit(log, "engine_root_self_missing")
	default:
		emit(log, "engine_root", "source", "self", "root", root)
		return root
	}
	if root := fromJSON(); root != "" {
		emit(log, "engine_root", "source", "engine.json", "root", root)
		return root
	}
	emit(log, "engine_root_missing")
	return ""
}

// selfRoot 自解析：从 exe 的真实路径（剥 \\?\ 前缀）所在目录往上认仓库根；
// 拿不到真实路径就退 os.Readlink(exe 所在目录)（目录联接的目标）再认。
func selfRoot() (string, error) {
	exe, err := os.Executable()
	if err != nil {
		return "", err
	}
	real, realErr := finalPathName(exe)
	if realErr == nil {
		return rootFromExeDir(filepath.Dir(real)), nil
	}
	target, linkErr := os.Readlink(filepath.Dir(exe))
	if linkErr != nil {
		return "", fmt.Errorf("真实路径失败（%v），读联接也失败（%v）", realErr, linkErr)
	}
	return rootFromExeDir(stripLongPrefix(target)), nil
}

// rootWalkUp 是最多往上认几级。两种摆法都要罩住：插件那份在
// <仓库根>\plugins\voice-pill\（两级），开发那份在 <仓库根>\bin\（一级）。
const rootWalkUp = 4

// rootFromExeDir：从 exe 所在目录往上认仓库根。不写死级数——两种摆法差一级，
// 写死必然错一种。认的是标记：看门狗要跑的 src\supervise.py 和引擎
// src\main.py 都在才算数。认不出就回 ""，交给下一级 engine.json（install.py
// 会写下正确的根）。认错根比认不出更糟：会拿别人的 supervise.py 起一个不是
// 这个仓库的引擎。
func rootFromExeDir(dir string) string {
	for i := 0; i < rootWalkUp; i++ {
		if isFile(filepath.Join(dir, "src", "main.py")) && isFile(filepath.Join(dir, "src", "supervise.py")) {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	return ""
}

// finalPathName 用 GetFinalPathNameByHandle 拿真实路径（走目录联接时返回
// 联接背后的真实位置），并剥掉 \\?\ 前缀。
func finalPathName(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	buf := make([]uint16, 1024)
	for {
		n, err := windows.GetFinalPathNameByHandle(windows.Handle(f.Fd()), &buf[0], uint32(len(buf)), 0)
		if err != nil {
			return "", err
		}
		if int(n) >= len(buf) {
			buf = make([]uint16, n+1)
			continue
		}
		return stripLongPrefix(windows.UTF16ToString(buf[:n])), nil
	}
}

func stripLongPrefix(p string) string {
	switch {
	case strings.HasPrefix(p, `\\?\UNC\`):
		return `\\` + p[len(`\\?\UNC\`):]
	case strings.HasPrefix(p, `\\?\`):
		return p[len(`\\?\`):]
	case strings.HasPrefix(p, `\??\`):
		return p[len(`\??\`):]
	}
	return p
}

// jsonRoot 读 %LOCALAPPDATA%\VoicePill\engine.json 的 {"root": "..."}。
func jsonRoot() string {
	raw, err := os.ReadFile(filepath.Join(bridge.AppDir(), "engine.json"))
	if err != nil {
		return ""
	}
	var cfg struct {
		Root string `json:"root"`
	}
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return ""
	}
	return strings.TrimSpace(cfg.Root)
}

// ---------- 看门狗：幂等拉起 ----------

func supervisorMutexName() string {
	if name := strings.TrimSpace(os.Getenv(SupervisorMutexEnv)); name != "" {
		return name
	}
	return SupervisorMutexName
}

// ProbeSupervisorMutex 非阻塞问一次「有没有人守着」。owned=true 表示刚才
// 没有别人（我们建了新对象并立即释放，等价于 Python InstanceLock 的
// acquire()+release()）。err 只在 CreateMutex 真失败时非 nil。
func ProbeSupervisorMutex() (owned bool, err error) {
	name, err := windows.UTF16PtrFromString(supervisorMutexName())
	if err != nil {
		return false, err
	}
	handle, err := windows.CreateMutex(nil, true, name)
	if err != nil {
		if errors.Is(err, windows.ERROR_ALREADY_EXISTS) {
			// 内核把已有互斥体的句柄给了我们，但所有权不在我们手上；
			// 不关掉就等于替它多留一个引用（对齐 Python 的注释）。
			if handle != 0 {
				windows.CloseHandle(handle)
			}
			return false, nil
		}
		return false, err
	}
	// 全新对象：我们持有，立刻放开（只用来判存在）。
	windows.ReleaseMutex(handle)
	windows.CloseHandle(handle)
	return true, nil
}

// SupervisorAlive 看门狗还在不在守（拿得到锁 = 没人在守）。问不出来就不
// 自作主张，当作「在守」——对齐 Python。
func SupervisorAlive() bool {
	owned, err := ProbeSupervisorMutex()
	if err != nil {
		return true
	}
	return !owned
}

// spawnSupervisor 是「真正把 pythonw 拉起来」的那一步，做成变量是为了让单测
// 注入假实现——测试绝不许真起进程（尤其不许碰正在跑的看门狗）。
var spawnSupervisor = func(pythonw, supervise, root string) error {
	cmd := exec.Command(pythonw, supervise)
	cmd.Dir = root
	cmd.SysProcAttr = &syscall.SysProcAttr{
		CreationFlags: windows.DETACHED_PROCESS | windows.CREATE_NO_WINDOW,
	}
	devnull, err := os.OpenFile(os.DevNull, os.O_RDWR, 0)
	if err != nil {
		return err
	}
	defer devnull.Close()
	cmd.Stdin, cmd.Stdout, cmd.Stderr = devnull, devnull, devnull
	if err := cmd.Start(); err != nil {
		return err
	}
	_ = cmd.Process.Release()
	return nil
}

// EnsureSupervisor 幂等确保看门狗在跑。返回 true 表示这次是我们亲手拉起来的。
//
// 与 Python hook_session_start._ensure_supervisor 一致：
//   - 已经有人在守 → 什么都不做；
//   - <root>\src\supervise.py 不存在 → 只记日志，不拉（自测靠这一条避开真身）；
//   - 否则用 DETACHED_PROCESS + CREATE_NO_WINDOW 起 pythonw supervise.py。
func EnsureSupervisor(root string, log Logger) bool {
	owned, err := ProbeSupervisorMutex()
	if err != nil {
		emit(log, "supervisor_probe_error", "error", err)
		return false
	}
	if !owned {
		emit(log, "supervisor_already_running")
		return false
	}
	supervise := filepath.Join(root, "src", "supervise.py")
	if root == "" || !isFile(supervise) {
		emit(log, "supervise_missing", "path", supervise)
		return false
	}
	pythonw := filepath.Join(root, ".venv", "Scripts", "pythonw.exe")
	if err := spawnSupervisor(pythonw, supervise, root); err != nil {
		emit(log, "supervisor_spawn_failed", "error", err)
		return false
	}
	emit(log, "supervisor_spawned")
	return true
}

// ---------- 等就绪 ----------

// Prober 探一次控制面：返回 (data, nil) 表示引擎已经在应答。
type Prober func(timeout time.Duration) (json.RawMessage, error)

// WaitReady 复刻 hook_session_start._wait_ready：≤6 秒，每 0.3 秒探一次
// status。「看门狗不在了就收手」必须等先见过它活着才算数——刚 spawn 出来
// 那一瞬它还没拿到互斥体（实测踩过）。
func WaitReady(prober Prober, log Logger) bool {
	return waitReady(prober, SupervisorAlive, log)
}

func waitReady(prober Prober, alive func() bool, log Logger) bool {
	deadline := time.Now().Add(readyWait)
	seenAlive := false
	for time.Now().Before(deadline) {
		if _, err := prober(time.Second); err == nil {
			return true
		}
		if alive() {
			seenAlive = true
		} else if seenAlive {
			emit(log, "ready_timeout", "reason", "supervisor_gone")
			return false
		}
		time.Sleep(readyPoll)
	}
	emit(log, "ready_timeout")
	return false
}

// Ensure 复刻 mcp_server._ensure_engine()：引擎没在跑就把它拉起来，返回拉起
// 后的状态。找不到引擎根或启动器时返回人话错误。
func Ensure(prober Prober, log Logger) (json.RawMessage, error) {
	if data, err := prober(ensureProbeTimeout); err == nil {
		return data, nil
	}
	root := Root(log)
	pythonw := filepath.Join(root, ".venv", "Scripts", "pythonw.exe")
	supervise := filepath.Join(root, "src", "supervise.py")
	if root == "" || !isFile(pythonw) || !isFile(supervise) {
		return nil, fmt.Errorf("Voice Pill 没在运行，而且找不到它的启动器：%s / %s", pythonw, supervise)
	}
	EnsureSupervisor(root, log) // 幂等：已经有看门狗在守就什么都不做
	deadline := time.Now().Add(ensureWait)
	for time.Now().Before(deadline) {
		if data, err := prober(ensurePollTimeout); err == nil {
			return data, nil
		}
		time.Sleep(ensurePollInterval)
	}
	return nil, errors.New("拉起 Voice Pill 之后 20 秒仍然连不上它的控制面。")
}

// ---------- 锚点纸条 ----------

// FindAnchor 找「最外层的 Codex 进程」，返回 (pid, image)；找不到 ok=false。
//
// 与 Python anchor.find_anchor() 同一套判据：父链最多 16 层，认
// codex.exe / chatgpt.exe（大小写不敏感），取最外层那个。
func FindAnchor() (pid int, image string, ok bool) {
	return FindAnchorFrom(os.Getpid())
}

// FindAnchorFrom 从**任意**进程出发往上找最外层那个 Codex。
//
// 为什么要能换起点：钩子是 Codex 的子孙，走自己的父链天经地义；哨兵不是
// （它由登录拉起，父链里没有 Codex），只能先扫出候选进程，再各自往上认。
// 判据一字不改，仍是 findAnchor 那一套。
func FindAnchorFrom(start int) (int, string, bool) {
	return findAnchor(start, processParent, processImage)
}

func findAnchor(start int, parent func(int) int, image func(int) string) (int, string, bool) {
	type match struct {
		pid   int
		image string
	}
	var matches []match
	seen := map[int]bool{}
	pid := start
	for hop := 0; hop < MaxHops; hop++ {
		if pid <= 0 || seen[pid] {
			break
		}
		seen[pid] = true
		parentPID := parent(pid)
		if parentPID <= 0 {
			break
		}
		img := image(parentPID)
		if isCodexImage(img) {
			matches = append(matches, match{pid: parentPID, image: img})
		}
		pid = parentPID
	}
	if len(matches) == 0 {
		return 0, "", false
	}
	outermost := matches[len(matches)-1]
	return outermost.pid, outermost.image, true
}

// IsCodexImage 与内部判据同一把尺子（按文件名比，大小写不敏感）。
func IsCodexImage(path string) bool { return isCodexImage(path) }

func isCodexImage(path string) bool {
	name := filepath.Base(path)
	for _, want := range CodexImageNames {
		if strings.EqualFold(name, want) {
			return true
		}
	}
	return false
}

var procNtQueryInformationProcess = windows.NewLazySystemDLL("ntdll.dll").NewProc("NtQueryInformationProcess")

// processBasicInformation 是 NtQueryInformationProcess(ProcessBasicInformation)
// 要的结构。前几个字段只为对齐：真用到的是最后那个父 PID，64 位下偏移 40，
// 与 Python 的 ctypes 布局一致（Reserved1 用 uintptr 顶替 DWORD + 4 字节填充）。
type processBasicInformation struct {
	Reserved1                    uintptr
	PebBaseAddress               uintptr
	Reserved2                    [2]uintptr
	UniqueProcessId              uintptr
	InheritedFromUniqueProcessId uintptr
}

// processParent 取父 PID。拿不到（不存在 / 是系统进程 / 查询失败）返回 0。
// 用 NtQueryInformationProcess，与 Python 侧一致；不采用 Toolhelp32——换成
// 它必须先与 Python 对拍（spec 第 9 节）。
func processParent(pid int) int {
	if pid <= 0 {
		return 0
	}
	handle, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return 0
	}
	defer windows.CloseHandle(handle)
	var info processBasicInformation
	var written uint32
	status, _, _ := procNtQueryInformationProcess.Call(
		uintptr(handle), 0,
		uintptr(unsafe.Pointer(&info)),
		unsafe.Sizeof(info),
		uintptr(unsafe.Pointer(&written)))
	if int32(status) != 0 {
		return 0
	}
	return int(info.InheritedFromUniqueProcessId)
}

// ProcessImage 是按 pid 取映像路径的对外口（判活时核「还是不是那个人」用）。
func ProcessImage(pid int) string { return processImage(pid) }

// processImage 是进程的完整映像路径。拿不到返回空串（对齐 Python）。
func processImage(pid int) string {
	if pid <= 0 {
		return ""
	}
	handle, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return ""
	}
	defer windows.CloseHandle(handle)
	buf := make([]uint16, 1024)
	size := uint32(len(buf))
	if err := windows.QueryFullProcessImageName(handle, 0, &buf[0], &size); err != nil {
		return ""
	}
	return windows.UTF16ToString(buf[:size])
}

// Note 是锚点纸条的内容，字段与 Python 侧 anchor.write_note 一致。
type Note struct {
	PID   int     `json:"pid"`
	Image string  `json:"image"`
	At    float64 `json:"at"`
}

// NotePath 是锚点纸条，与 Python 侧 anchor.note_path() 同一个文件。
func NotePath() string { return filepath.Join(bridge.AppDir(), "anchor.json") }

// ReadNote 读纸条。读不到 / 格式不对 / pid 非法 → ok=false（调用方当
// 「没有锚点」处理）。
func ReadNote() (Note, bool) {
	raw, err := os.ReadFile(NotePath())
	if err != nil {
		return Note{}, false
	}
	var note Note
	if err := json.Unmarshal(raw, &note); err != nil {
		return Note{}, false
	}
	if note.PID <= 0 {
		return Note{}, false
	}
	return note, true
}

// WriteNote 写纸条：pid + image 没变就一个字节都不动（免得白刷 mtime）；
// 变了就写 .tmp 再原子替换，对齐 Python anchor.write_note。
func WriteNote(pid int, image string) bool {
	if pid <= 0 {
		return false
	}
	if note, ok := ReadNote(); ok && note.PID == pid && note.Image == image {
		return true
	}
	path := NotePath()
	if err := os.MkdirAll(filepath.Dir(path), 0o777); err != nil {
		return false
	}
	raw, err := json.Marshal(Note{
		PID:   pid,
		Image: image,
		At:    float64(time.Now().UnixNano()) / 1e9,
	})
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

// WriteAnchor 把「我在谁的子孙里」写下来。找不到锚点就只记日志，不拦路。
func WriteAnchor(log Logger) (pid int, image string, ok bool) {
	foundPID, foundImage, found := FindAnchor()
	if !found {
		emit(log, "anchor_missing")
		return 0, "", false
	}
	if !WriteNote(foundPID, foundImage) {
		emit(log, "anchor_write_failed", "pid", foundPID)
		return foundPID, foundImage, false
	}
	emit(log, "anchor", "pid", foundPID, "image", filepath.Base(foundImage))
	return foundPID, foundImage, true
}

// ---------- 小工具 ----------

func isFile(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular()
}

func errText(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}
