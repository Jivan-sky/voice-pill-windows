package sentinel

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"voicepill/internal/engine"
)

// 单测的原则：五个口（Scan / Anchor / Note / Bind / Alive）全换成假的。
// 自测绝不许真扫进程、真写纸条、真拉看门狗、真碰注册表。

type fakeState struct {
	mu      sync.Mutex
	apps    []app
	alive   bool
	note    engine.Note
	hasNote bool
	binds   []string
	events  []string

	// supDead=true 表示「看门狗不在守」；默认 false（有人在守）。
	supDead bool
	// stopped / stopPID 是停用纸那一路（默认没有纸）。
	stopped bool
	stopPID int
	// bindFail=true 让补拉失败，验「一次死亡只补一次」。
	bindFail bool
}

func newFake(state *fakeState) *Watcher {
	w := &Watcher{
		Log: func(event string, fields ...any) {
			state.mu.Lock()
			defer state.mu.Unlock()
			state.events = append(state.events, fmt.Sprint(event))
		},
		Poll:  time.Millisecond,
		Every: time.Millisecond,
		Scan: func() []int {
			state.mu.Lock()
			defer state.mu.Unlock()
			pids := make([]int, 0, len(state.apps))
			for _, a := range state.apps {
				pids = append(pids, a.pid)
			}
			return pids
		},
		Anchor: func(pid int) (int, string, bool) {
			state.mu.Lock()
			defer state.mu.Unlock()
			for _, a := range state.apps {
				if a.pid == pid {
					return a.pid, a.image, true
				}
			}
			return 0, "", false
		},
		Note: func() (engine.Note, bool) {
			state.mu.Lock()
			defer state.mu.Unlock()
			return state.note, state.hasNote
		},
		Alive: func(pid int) bool {
			state.mu.Lock()
			defer state.mu.Unlock()
			if !state.alive {
				return false
			}
			for _, a := range state.apps {
				if a.pid == pid {
					return true
				}
			}
			return false
		},
		SupervisorAlive: func() bool {
			state.mu.Lock()
			defer state.mu.Unlock()
			return !state.supDead
		},
		Stopped: func() (bool, int) {
			state.mu.Lock()
			defer state.mu.Unlock()
			return state.stopped, state.stopPID
		},
	}
	w.Bind = func(pid int, image string) bool {
		state.mu.Lock()
		defer state.mu.Unlock()
		if state.bindFail {
			return false
		}
		state.binds = append(state.binds, fmt.Sprintf("%d:%s", pid, image))
		state.note = engine.Note{PID: pid, Image: image}
		state.hasNote = true
		return true
	}
	return w
}

func (s *fakeState) snapshot() (binds []string, events []string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string{}, s.binds...), append([]string{}, s.events...)
}

func (s *fakeState) countEvent(event string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	n := 0
	for _, e := range s.events {
		if e == event {
			n++
		}
	}
	return n
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatalf("等不到：%s", what)
}

func TestTickBindsNewAppOnce(t *testing.T) {
	state := &fakeState{apps: []app{{pid: 100, image: "chatgpt.exe"}}, alive: true}
	w := newFake(state)

	pid, bound := w.Tick()
	if pid != 100 || !bound {
		t.Fatalf("第一轮该认下 100 并绑定，得到 pid=%d bound=%v", pid, bound)
	}
	pid, bound = w.Tick()
	if pid != 100 || bound {
		t.Fatalf("纸条写的就是它，第二轮不该再绑：pid=%d bound=%v", pid, bound)
	}
	binds, events := state.snapshot()
	if len(binds) != 1 || binds[0] != "100:chatgpt.exe" {
		t.Fatalf("只许绑一次，得到 %v", binds)
	}
	if got := countOf(events, "sentinel_app"); got != 1 {
		t.Fatalf("sentinel_app 只该记一次，得到 %d 次：%v", got, events)
	}
	// 这一轮是哨兵自己绑的，第二轮只是「无事可做」——不该再记一条，
	// 更不该每 2 秒刷一行日志。
	if got := countOf(events, "sentinel_note_kept"); got != 0 {
		t.Fatalf("自己绑的那一轮不该记 note_kept，得到 %d 次：%v", got, events)
	}
}

func TestTickKeepsNoteWrittenByHook(t *testing.T) {
	// 钩子先写过纸条：哨兵不许再拉一遍（也不许刷日志）。
	state := &fakeState{
		apps:    []app{{pid: 7, image: "codex.exe"}},
		alive:   true,
		note:    engine.Note{PID: 7, Image: "codex.exe"},
		hasNote: true,
	}
	w := newFake(state)
	for i := 0; i < 3; i++ {
		if _, bound := w.Tick(); bound {
			t.Fatalf("第 %d 轮不该绑", i)
		}
	}
	binds, events := state.snapshot()
	if len(binds) != 0 {
		t.Fatalf("一次都不该绑，得到 %v", binds)
	}
	if got := countOf(events, "sentinel_note_kept"); got != 1 {
		t.Fatalf("note_kept 只该记一次（别刷日志），得到 %d：%v", got, events)
	}
}

func TestTickNoApp(t *testing.T) {
	state := &fakeState{alive: true}
	w := newFake(state)
	if pid, bound := w.Tick(); pid != 0 || bound {
		t.Fatalf("没有 Codex 时该返回 0/false，得到 %d/%v", pid, bound)
	}
	if _, events := state.snapshot(); len(events) != 0 {
		t.Fatalf("没有 Codex 不该记日志：%v", events)
	}
}

func TestTickIgnoresDeadApp(t *testing.T) {
	// 进程表里还挂着、但已经断气的号——不许写锚点（写了也是白拉一次）。
	state := &fakeState{apps: []app{{pid: 9, image: "chatgpt.exe"}}, alive: false}
	w := newFake(state)
	if pid, bound := w.Tick(); pid != 0 || bound {
		t.Fatalf("断气的号不该被认下来，得到 pid=%d bound=%v", pid, bound)
	}
	if binds, _ := state.snapshot(); len(binds) != 0 {
		t.Fatalf("断气的号不该触发绑定：%v", binds)
	}
}

func TestFindPrefersDesktopShell(t *testing.T) {
	state := &fakeState{apps: []app{
		{pid: 11, image: "C:\\x\\codex.exe"},
		{pid: 22, image: "C:\\x\\ChatGPT.exe"},
	}, alive: true}
	w := newFake(state)
	pid, image, ok := w.find()
	if !ok || pid != 22 || !strings.EqualFold(image, "C:\\x\\ChatGPT.exe") {
		t.Fatalf("该选桌面壳，得到 pid=%d image=%q ok=%v", pid, image, ok)
	}
}

func TestWatchAppLogsGoneAndResetsLast(t *testing.T) {
	state := &fakeState{apps: []app{{pid: 5, image: "chatgpt.exe"}}, alive: true}
	w := newFake(state)
	w.last = 5
	go func() {
		time.Sleep(5 * time.Millisecond)
		state.mu.Lock()
		state.alive = false
		state.mu.Unlock()
	}()
	w.watchApp(5)
	if w.last != 0 {
		t.Fatalf("应用走了该把 last 清掉，得到 %d", w.last)
	}
	if got := state.countEvent("sentinel_app_gone"); got != 1 {
		t.Fatalf("该记一次 app_gone，得到 %d", got)
	}
}

func TestWatchRebindsAfterRestart(t *testing.T) {
	state := &fakeState{apps: []app{{pid: 100, image: "chatgpt.exe"}}, alive: true}
	w := newFake(state)
	stop := make(chan struct{})
	w.Stop = stop
	done := make(chan struct{})
	go func() {
		defer close(done)
		w.Watch()
	}()

	waitFor(t, "第一次绑定", func() bool {
		binds, _ := state.snapshot()
		return len(binds) == 1
	})
	// 第一个 Codex 退了，换一个新的（新 pid）——必须再绑一次。
	state.mu.Lock()
	state.alive = false
	state.mu.Unlock()
	waitFor(t, "记下 app_gone", func() bool { return state.countEvent("sentinel_app_gone") == 1 })
	state.mu.Lock()
	state.apps = []app{{pid: 200, image: "chatgpt.exe"}}
	state.alive = true
	state.mu.Unlock()
	waitFor(t, "第二次绑定", func() bool {
		binds, _ := state.snapshot()
		return len(binds) == 2
	})
	binds, _ := state.snapshot()
	if binds[1] != "200:chatgpt.exe" {
		t.Fatalf("第二次该绑新 pid，得到 %v", binds)
	}
	close(stop)
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("Stop 关掉之后 Watch 没退出来")
	}
}

// 看门狗自己崩了、Codex 还开着：哨兵该把它补拉回来（纸条上写着它才补）。
func TestWatchAppRevivesSupervisor(t *testing.T) {
	state := &fakeState{
		apps:    []app{{pid: 100, image: "chatgpt.exe"}},
		alive:   true,
		note:    engine.Note{PID: 100, Image: "chatgpt.exe"},
		hasNote: true,
		supDead: true,
	}
	w := newFake(state)
	stop, done := make(chan struct{}), make(chan struct{})
	w.Stop = stop
	go func() { defer close(done); w.Watch() }()

	waitFor(t, "补拉一次", func() bool { return state.countEvent("sentinel_revive") == 1 })
	waitFor(t, "按纸条上的 pid 重绑", func() bool {
		binds, _ := state.snapshot()
		return len(binds) == 1
	})
	if binds, _ := state.snapshot(); binds[0] != "100:chatgpt.exe" {
		t.Fatalf("补拉该用纸条上的 pid 与映像，得到 %v", binds)
	}

	close(stop)
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("Stop 关掉之后 Watch 没退出来")
	}
}

// 用户明确停过这一代 Codex（纸条在、Codex 还开着）：不许偷偷拉回来。
func TestWatchAppDoesNotReviveAfterStop(t *testing.T) {
	state := &fakeState{
		apps:    []app{{pid: 100, image: "chatgpt.exe"}},
		alive:   true,
		note:    engine.Note{PID: 100, Image: "chatgpt.exe"},
		hasNote: true,
		supDead: true,
		stopped: true,
		stopPID: 100,
	}
	w := newFake(state)
	stop, done := make(chan struct{}), make(chan struct{})
	w.Stop = stop
	go func() { defer close(done); w.Watch() }()

	waitFor(t, "记下被拦住", func() bool { return state.countEvent("sentinel_revive_blocked") == 1 })
	time.Sleep(30 * time.Millisecond)
	if got := state.countEvent("sentinel_revive"); got != 0 {
		t.Fatalf("停用纸在就不该补拉，得到 %d 次 revive", got)
	}
	if binds, _ := state.snapshot(); len(binds) != 0 {
		t.Fatalf("停用纸在就不该重绑，得到 %v", binds)
	}

	close(stop)
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("Stop 关掉之后 Watch 没退出来")
	}
}

// 补拉失败的原因都是持续性的（supervise.py 不见了之类），不许每秒硬撞。
func TestWatchAppRevivesOncePerDeath(t *testing.T) {
	state := &fakeState{
		apps:     []app{{pid: 100, image: "chatgpt.exe"}},
		alive:    true,
		note:     engine.Note{PID: 100, Image: "chatgpt.exe"},
		hasNote:  true,
		supDead:  true,
		bindFail: true,
	}
	w := newFake(state)
	stop, done := make(chan struct{}), make(chan struct{})
	w.Stop = stop
	go func() { defer close(done); w.Watch() }()

	waitFor(t, "补拉一次", func() bool { return state.countEvent("sentinel_revive") == 1 })
	time.Sleep(40 * time.Millisecond)
	if got := state.countEvent("sentinel_revive"); got != 1 {
		t.Fatalf("一次死亡只补一次，得到 %d 次 revive", got)
	}

	close(stop)
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("Stop 关掉之后 Watch 没退出来")
	}
}

func TestFakeAppsParsing(t *testing.T) {
	// 设了空值 = 确定没有 Codex（自测要的是确定，不许退回真扫描）。
	t.Setenv(FakeAppEnv, " ")
	if apps, ok := fakeApps(); !ok || len(apps) != 0 {
		t.Fatalf("设了空值该表示「确定没有 Codex」，得到 %v ok=%v", apps, ok)
	}
	t.Setenv(FakeAppEnv, "4242:chatgpt.exe, 7:codex.exe, 9:notepad.exe, 破")
	apps, ok := fakeApps()
	if !ok || len(apps) != 2 {
		t.Fatalf("该认下两条 Codex、丢掉 notepad 与破格式，得到 %v", apps)
	}
	if apps[0].pid != 4242 || apps[1].pid != 7 {
		t.Fatalf("顺序与内容不对：%v", apps)
	}
}

func TestCommandQuotesExe(t *testing.T) {
	exe := "D:\\Own_tools&skills\\voice-pill-windows\\plugins\\voice-pill\\voicepill.exe"
	got := Command(exe)
	want := "\"" + exe + "\" sentinel"
	if got != want {
		t.Fatalf("登记的那一行不对：%s", got)
	}
}

// fakeStore 是 RunStore 的假实现：一个 map。
type fakeStore struct {
	path   string
	values map[string]string
	fail   error
}

func (s *fakeStore) Path() string { return s.path }

func (s *fakeStore) Get(name string) (string, bool, error) {
	if s.fail != nil {
		return "", false, s.fail
	}
	value, ok := s.values[name]
	return value, ok, nil
}

func (s *fakeStore) Set(name, value string) error {
	if s.fail != nil {
		return s.fail
	}
	s.values[name] = value
	return nil
}

func (s *fakeStore) Delete(name string) error {
	if s.fail != nil {
		return s.fail
	}
	delete(s.values, name)
	return nil
}

func TestInstallUninstall(t *testing.T) {
	store := &fakeStore{path: "Software\\VoicePill\\Test", values: map[string]string{}}
	var out bytes.Buffer
	if code := Install(store, "C:\\x\\voicepill.exe", &out); code != 0 {
		t.Fatalf("Install 退出码该是 0，得到 %d：%s", code, out.String())
	}
	if store.values[RunValueName] != "\"C:\\x\\voicepill.exe\" sentinel" {
		t.Fatalf("登记内容不对：%q", store.values[RunValueName])
	}
	if !strings.Contains(out.String(), "已登记登录自启") {
		t.Fatalf("该说清楚登记到哪儿了：%s", out.String())
	}

	out.Reset()
	if code := Uninstall(store, &out); code != 0 {
		t.Fatalf("Uninstall 退出码该是 0，得到 %d", code)
	}
	if _, ok := store.values[RunValueName]; ok {
		t.Fatal("卸载之后不该还留着那一行")
	}

	// 登记失败要如实报出来（不许装作成功）。
	bad := &fakeStore{path: "Software\\VoicePill\\Test", values: map[string]string{}, fail: errors.New("拒绝访问")}
	out.Reset()
	if code := Install(bad, "C:\\x\\voicepill.exe", &out); code != 1 {
		t.Fatalf("登记失败该返回 1，得到 %d", code)
	}
	if !strings.Contains(out.String(), "登记失败") {
		t.Fatalf("该把失败说出来：%s", out.String())
	}
}

func TestOnceReportsBothWays(t *testing.T) {
	state := &fakeState{apps: []app{{pid: 4242, image: "chatgpt.exe"}}, alive: true}
	w := newFake(state)
	var out bytes.Buffer
	if code := once(w, &out); code != 0 {
		t.Fatalf("--once 该返回 0，得到 %d", code)
	}
	if !strings.Contains(out.String(), "pid=4242") {
		t.Fatalf("该说认下了谁：%s", out.String())
	}

	out.Reset()
	if code := once(newFake(&fakeState{alive: true}), &out); code != 0 {
		t.Fatalf("没有 Codex 时 --once 也该返回 0，得到 %d", code)
	}
	if !strings.Contains(out.String(), "没找到") {
		t.Fatalf("该说没找到：%s", out.String())
	}
}

func countOf(events []string, want string) int {
	n := 0
	for _, e := range events {
		if e == want {
			n++
		}
	}
	return n
}

// 纸条上的 exe 必须先归一到真实路径：插件是目录联接装的，不归一 --status 会说
// 「没在跑」（实测），--stop 也会拒绝动手。
func TestRecordSelfNormalizesExePath(t *testing.T) {
	t.Setenv("LOCALAPPDATA", t.TempDir())
	old := resolveExePath
	resolveExePath = func(string) string { return "D:\\repo\\plugins\\voice-pill\\voicepill.exe" }
	defer func() { resolveExePath = old }()

	if !recordSelf() {
		t.Fatal("记账失败")
	}
	rec, ok := ReadRecord()
	if !ok {
		t.Fatal("读不回来")
	}
	if rec.Exe != "D:\\repo\\plugins\\voice-pill\\voicepill.exe" {
		t.Fatalf("纸条上的 exe 没归一：%q", rec.Exe)
	}
	if rec.PID != os.Getpid() {
		t.Fatalf("纸条上的 pid 不是自己：%d", rec.PID)
	}
}
