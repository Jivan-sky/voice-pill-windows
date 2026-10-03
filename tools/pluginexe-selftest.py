# -*- coding: utf-8 -*-
"""假引擎 + 真 exe 的端到端自测：8 个 MCP 工具 + 3 个钩子 + tools/list 契约。

跑的是提交进 git 的那份 `bin\\voicepill.exe`；管道名 / 引擎根 / LOCALAPPDATA /
看门狗互斥体全走测试专用值，**绝不碰正在跑的引擎**（真身此刻可能活着）。

为什么需要它
------------
前三条自测各自盯一段：`bridge-selftest.py` 走旧的控制面，
`bridge-json-selftest.py` 走新的 NDJSON 控制面，`supervise-selftest.py` 管看门狗。
**没有一条**把「Codex 真要用的那个 exe」整条链路串起来——SDK 反射出来的 schema、
八个工具的名字与描述、stdio 分帧、钩子的一行 JSON、Stop 的 4000 字截断。
这把尺子就盯这些：用假引擎把控制面接过来，拿真 exe 走一遍。

gold 基线
---------
`tools\\pluginexe-tools-gold.json` 是**冻结的 Python 契约**：八个工具的名字、
描述（逐字节）、inputSchema（解析后语义相等）。两份 SDK 生成的 schema 天生
表示不同（Go 多 `additionalProperties`、空 `properties` 可能省略），所以比的是
「抹平表示差异后逐字段相等」，描述则是逐字节相等。

gold 由 Python 版生成（用有 mcp 的那个 venv），生成命令见
`docs/superpowers/plans/2026-10-03-插件换Go单exe.md` 任务 10。
任务 18 删掉 Python 实现之后，这份 gold 就是唯一基线。

用法（任意 cwd 都行）：
    .venv\\Scripts\\python.exe tools\\pluginexe-selftest.py
失败返回非零——可以当门禁用。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(REPO_ROOT, "bin", "voicepill.exe")
GOLD_PATH = os.path.join(REPO_ROOT, "tools", "pluginexe-tools-gold.json")
SRC_DIR = os.path.join(REPO_ROOT, "src")

sys.path.insert(0, SRC_DIR)
import console                      # noqa: E402

console.make_output_safe()

import bridge                       # noqa: E402

# 与 Go 侧 internal/tools、internal/hooks 里那两句一字不差。冻结在尺子里，
# 就是「正文逐字对齐」这条的判据。
PROMPT_PREFIX = (
    "用户刚刚用语音输入（Voice Pill）说了以下内容，"
    "请把它当作本轮意图的一部分：\n"
)

# 计划表里写死的两句话，逐字对齐 Python 版。
NOTE_NOT_RUNNING = "常驻引擎没在跑。调用 voice_pill_listen 会自动把它拉起来。"
SPEAK_EMPTY = "要念的文本是空的。"

PROTOCOL_VERSION = "2025-06-18"


class Checker:
    def __init__(self) -> None:
        self.failed = 0
        self.total = 0

    def __call__(self, label: str, ok: bool, detail: str = "") -> bool:
        self.total += 1
        mark = "✅" if ok else "❌"
        print("  %s %s%s" % (mark, label,
                             "" if ok else ("  ← %s" % detail if detail else "")))
        if not ok:
            self.failed += 1
        return ok


def title(fn) -> str:
    doc = (fn.__doc__ or fn.__name__).strip()
    return doc.splitlines()[0]


def _wait(pred, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return bool(pred())


def _require_test_pipe() -> None:
    """起假引擎之前，确认用的还是**测试专用**管道名。

    名字一旦退回真身那个，假引擎会因 FIRST_PIPE_INSTANCE 建不起来，而 exe
    会**连上真身**（take 取走用户的话、stop 停掉真身）。真身此刻可能正在跑。
    """
    name = bridge.json_pipe_name()
    if "selftest" not in name:
        raise SystemExit(
            "自测中止：管道名是 %r，它会连到真身上。本自测必须在测试专用"
            "管道名下运行。" % name)


# ---------- 假引擎 ----------

PID = 4321


class FakeEngine:
    """七命令的桩 handler，包在工程已审过的 bridge.JsonBridgeServer 里。"""

    def __init__(self) -> None:
        self.seen: list = []                # [(cmd, args)]
        self.phase = "idle"
        self.take_texts: list = []
        self.recording_left = None          # start 之后还能回几次 recording
        self._server = bridge.JsonBridgeServer(self._handler)

    def start(self) -> "FakeEngine":
        self._server.start()
        return self

    def stop(self) -> None:
        self._server.stop()

    @property
    def alive(self) -> bool:
        return self._server.alive

    @property
    def started(self) -> bool:
        return self._server.started

    @property
    def error(self):
        return self._server.error

    def reset(self, **kwargs) -> None:
        self.seen = []
        for key, value in kwargs.items():
            setattr(self, key, value)

    def cmds(self) -> list:
        return [cmd for cmd, _ in self.seen]

    def _handler(self, cmd: str, args: dict):
        args = args or {}
        self.seen.append((cmd, args))
        if cmd == "status":
            if self.recording_left is not None:
                if self.recording_left <= 0:
                    self.phase = "idle"
                else:
                    self.recording_left -= 1
            return {"pid": PID, "phase": self.phase, "backend": "local",
                    "hotkey": "Fn", "pending": len(self.take_texts)}
        if cmd == "take":
            texts = list(self.take_texts)
            self.take_texts = []
            return {"pid": PID, "phase": self.phase, "texts": texts}
        if cmd == "start":
            self.phase = "recording"
            self.recording_left = 1
            return {"pid": PID, "phase": self.phase}
        if cmd == "speak":
            return {"pid": PID, "phase": "speaking",
                    "text": args.get("text"), "auto": args.get("auto")}
        if cmd in ("stop", "cancel"):
            self.phase = "idle"
            self.recording_left = None
            return {"pid": PID, "phase": self.phase}
        if cmd == "shutup":
            return {"pid": PID, "phase": "idle"}
        return {"pid": PID, "phase": self.phase}


# ---------- MCP stdio 客户端（手写，够用就好）----------

class MCPClient:
    """往 exe 的 stdin 写 JSON-RPC 行、从 stdout 收行。

    不引 SDK：这份尺子要能独立证明「线上分帧就是一行一个 JSON」。
    """

    def __init__(self, exe: str, env: dict) -> None:
        self.proc = subprocess.Popen(
            [exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env)
        self._next_id = 0
        self._responses: dict = {}
        self._lock = threading.Lock()
        self.parse_errors: list = []
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.parse_errors.append(line)
                continue
            if isinstance(msg, dict) and "id" in msg:
                with self._lock:
                    self._responses[msg["id"]] = msg

    def _send(self, payload: dict) -> None:
        data = (json.dumps(payload, ensure_ascii=False,
                           separators=(",", ":")) + "\n").encode("utf-8")
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def request(self, method: str, params: dict, timeout: float = 20.0) -> dict:
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                    "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if rid in self._responses:
                    return self._responses.pop(rid)
            time.sleep(0.01)
        raise TimeoutError("等 %s 的回包超时（%.1fs）" % (method, timeout))

    def handshake(self) -> dict:
        hello = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "vp-pluginexe-selftest", "version": "1"},
        })
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return hello

    def close(self):
        """写完请求要留 300 毫秒再 EOF，否则回复会丢（实测两次，见计划注意事项）。"""
        time.sleep(0.35)
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            code = self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            code = self.proc.wait()
        stderr = self.proc.stderr.read().decode("utf-8", "replace")
        return code, stderr


def tool_text(result: dict) -> str:
    content = result.get("content") or []
    if not content:
        return ""
    return content[0].get("text") or ""


def tool_json(result: dict) -> dict:
    text = tool_text(result)
    try:
        return json.loads(text)
    except ValueError:
        return {"__unparsable__": text}


def call_tool(client: MCPClient, name: str, args: dict = None,
              timeout: float = 30.0) -> dict:
    return client.request("tools/call", {"name": name, "arguments": args or {}},
                          timeout)


def norm_schema(schema: dict) -> dict:
    """抹平两份 SDK 的表示差异：Go 多 `additionalProperties`；空 `properties`
    可能省略。抹平后应当逐字段相等。"""
    out = json.loads(json.dumps(schema))
    out.pop("additionalProperties", None)
    out.pop("$schema", None)
    if not out.get("properties"):
        out.pop("properties", None)
    return out


# ---------- 各段检查 ----------

def check_tools_list(ck: Checker, client: MCPClient, gold: list) -> None:
    """tools/list：8 个名字、描述逐字节、inputSchema 语义相等"""
    resp = client.request("tools/list", {})
    if not ck("tools/list 有 result", "result" in resp,
              json.dumps(resp, ensure_ascii=False)[:200]):
        return
    tools = resp["result"]["tools"]
    ck("一共 8 个工具", len(tools) == 8, len(tools))
    got_names = [t["name"] for t in tools]
    want_names = [g["name"] for g in gold]
    # go-sdk 的 tools/list 按名字排序，Python 按注册序——顺序不构成契约，
    # 比的是名字集合且不许有重复。
    ck("8 个名字与 Python 版一致（集合，无重复）",
       len(set(got_names)) == 8 and set(got_names) == set(want_names), got_names)

    by_name = {t["name"]: t for t in tools}
    bad_desc = [g["name"] for g in gold
                if (by_name.get(g["name"]) or {}).get("description") != g["description"]]
    ck("8 条 description 逐字节一致（真中文，不是 ??）", not bad_desc, bad_desc)

    bad_schema = []
    for g in gold:
        got = (by_name.get(g["name"]) or {}).get("inputSchema") or {}
        if norm_schema(got) != norm_schema(g["inputSchema"]):
            bad_schema.append("%s: got %s / want %s" % (
                g["name"],
                json.dumps(norm_schema(got), ensure_ascii=False, sort_keys=True),
                json.dumps(norm_schema(g["inputSchema"]), ensure_ascii=False,
                           sort_keys=True)))
    ck("8 条 inputSchema 解析后语义相等", not bad_schema, bad_schema)

    # 钉住 Python 签名里带默认值的字段（gold 里也有，但这里对 exe 再钉一次）。
    hook = (by_name["voice_pill_prompt_hook"]["inputSchema"] or {}).get("properties") or {}
    fields = ["hook_event_name", "session_id", "turn_id", "cwd", "prompt"]
    bad = [f for f in fields if hook.get(f, {}).get("default", "\x00") != ""]
    ck("prompt_hook 五个字段都钉着 default: \"\"", not bad, bad)
    seconds = ((by_name["voice_pill_listen"]["inputSchema"] or {}).get("properties") or {}).get("seconds") or {}
    ck("listen.seconds 是 number 且 default=10.0",
       seconds.get("type") == "number" and seconds.get("default") == 10.0, seconds)
    speak = by_name["voice_pill_speak"]["inputSchema"]
    ck("speak 只有 text 必填", speak.get("required") == ["text"], speak.get("required"))
    noargs = ["voice_pill_status", "voice_pill_stop", "voice_pill_cancel",
              "voice_pill_take", "voice_pill_shutup"]
    bad_noarg = [n for n in noargs
                 if norm_schema(by_name[n]["inputSchema"]) !=
                 {"title": n + "Arguments", "type": "object"}]
    ck("5 个无参工具 schema 就是空对象（+title）", not bad_noarg, bad_noarg)


def dump_tools_evidence(tools: list) -> None:
    """把线上回来的 8 个工具名与描述原文打出来（证据用）。"""
    print("\n--- tools/list 原文（8 个工具名 + 描述 + inputSchema）---")
    for tool in tools:
        print("name: %s" % tool["name"])
        print("description: %s" % json.dumps(tool["description"], ensure_ascii=False))
        print("inputSchema: %s" % json.dumps(tool["inputSchema"], ensure_ascii=False,
                                              sort_keys=True))
    print("--- end tools/list ---\n")


def check_tools_call(ck: Checker, client: MCPClient, engine: FakeEngine) -> None:
    """tools/call：八个工具逐个打一遍（假引擎在场）"""

    engine.reset(phase="idle", recording_left=None, take_texts=[])
    status = tool_json(call_tool(client, "voice_pill_status")["result"])
    ck("status：running=True", status.get("running") is True, status)
    ck("status：pid 来自假引擎", status.get("pid") == PID, status)

    engine.reset(phase="idle", recording_left=None, take_texts=["待取一", "待取二"])
    taken = call_tool(client, "voice_pill_take")["result"]
    data = tool_json(taken)
    ck("take：两条字原样回来", data.get("texts") == ["待取一", "待取二"], data)
    ck("take：只打了 take 一次", engine.cmds() == ["take"], engine.seen)

    engine.reset(phase="idle", recording_left=None, take_texts=[])
    empty = call_tool(client, "voice_pill_speak", {"text": "   "})["result"]
    ck("speak 空文本：isError=true", empty.get("isError") is True, empty)
    ck("speak 空文本：报「%s」" % SPEAK_EMPTY, SPEAK_EMPTY in tool_text(empty),
       tool_text(empty))
    ck("speak 空文本：没打到引擎", engine.cmds() == [], engine.seen)

    engine.reset(phase="idle", recording_left=None, take_texts=[])
    said = call_tool(client, "voice_pill_speak", {"text": "念 <b>&</b> 这段"})["result"]
    text = tool_text(said)
    ck("speak：< > & 没被 HTML 转义", "\\u003c" not in text and "\\u0026" not in text
       and "<b>&</b>" in text, text)
    ck("speak：structuredContent 解析后与 text 等价",
       said.get("structuredContent") == json.loads(text), said.get("structuredContent"))
    speaks = [a for c, a in engine.seen if c == "speak"]
    ck("speak：恰好打到引擎一次", len(speaks) == 1, engine.seen)
    ck("speak：text 原样送达", bool(speaks) and speaks[0].get("text") == "念 <b>&</b> 这段",
       speaks)
    ck("speak：auto=false", bool(speaks) and speaks[0].get("auto") is False, speaks)

    engine.reset(phase="recording", recording_left=None, take_texts=[])
    cancelled = tool_json(call_tool(client, "voice_pill_cancel")["result"])
    ck("cancel：先 cancel 再 status", engine.cmds() == ["cancel", "status"], engine.seen)
    ck("cancel：回来的是 status 数据（phase=idle）", cancelled.get("phase") == "idle",
       cancelled)

    engine.reset(phase="idle", recording_left=None, take_texts=[])
    call_tool(client, "voice_pill_shutup")
    ck("shutup：原样透传到引擎", engine.cmds() == ["shutup"], engine.seen)

    engine.reset(phase="idle", recording_left=None, take_texts=["停止后取到的字"])
    stopped = tool_json(call_tool(client, "voice_pill_stop")["result"])
    ck("stop：phase 已 idle 就不发 stop（对齐 Python）",
       "stop" not in engine.cmds(), engine.seen)
    ck("stop：text 是 take 的最后一条", stopped.get("text") == "停止后取到的字", stopped)
    ck("stop：timed_out=false", stopped.get("timed_out") is False, stopped)

    engine.reset(phase="idle", recording_left=None, take_texts=["listen 取到的字"])
    started_at = time.monotonic()
    listened = tool_json(call_tool(client, "voice_pill_listen", {})["result"])
    elapsed = time.monotonic() - started_at
    cmds = engine.cmds()
    ck("listen：默认 10 秒但没傻等（< 3 秒）", elapsed < 3.0, "%.2fs" % elapsed)
    ck("listen：轮询很少（桩第二次 status 就回 idle）", cmds.count("status") <= 8,
       "%d 次 status" % cmds.count("status"))
    ck("listen：链路 ensure→start→…→stop→take",
       bool(cmds) and cmds[0] == "status" and "start" in cmds and "stop" in cmds
       and cmds[-1] == "take" and cmds.index("start") < cmds.index("stop"), cmds)
    ck("listen：text 来自 take", listened.get("text") == "listen 取到的字", listened)

    engine.reset(phase="idle", recording_left=None,
                 take_texts=["语音一", "   ", "语音二"])
    hooked = tool_json(call_tool(client, "voice_pill_prompt_hook", {})["result"])
    spec = (hooked or {}).get("hookSpecificOutput") or {}
    ck("prompt_hook：hookEventName=UserPromptSubmit",
       spec.get("hookEventName") == "UserPromptSubmit", hooked)
    ck("prompt_hook：additionalContext 正文逐字对齐（空白条被滤掉）",
       spec.get("additionalContext") == PROMPT_PREFIX + "语音一\n语音二",
       spec.get("additionalContext"))


def check_hooks_prompt_submit(ck: Checker, engine: FakeEngine, env: dict) -> None:
    """prompt-submit 钩子：有字注入、没字放行"""
    engine.reset(phase="idle", recording_left=None, take_texts=["第一条语音", "  ", "第二条语音"])
    code, out, err = run_hook(env, "prompt-submit", "{}")
    lines = out.decode("utf-8", "replace").splitlines()
    ck("prompt-submit：退出码 0", code == 0, err.decode("utf-8", "replace"))
    ck("prompt-submit：stdout 恰好一行", len(lines) == 1, lines)
    payload = json.loads(lines[0]) if lines else {}
    spec = (payload or {}).get("hookSpecificOutput") or {}
    ck("prompt-submit：additionalContext 正文逐字对齐",
       spec.get("additionalContext") == PROMPT_PREFIX + "第一条语音\n第二条语音",
       payload)

    engine.reset(phase="idle", recording_left=None, take_texts=[])
    code, out, err = run_hook(env, "prompt-submit", json.dumps({"prompt": "在吗"}))
    lines = out.decode("utf-8", "replace").splitlines()
    ck("prompt-submit 空队列：stdout 恰好一行 {\"continue\": true}",
       code == 0 and len(lines) == 1 and json.loads(lines[0]) == {"continue": True},
       lines)


def check_hooks_stop(ck: Checker, engine: FakeEngine, env: dict) -> None:
    """stop 钩子：5000 字按**字符**截到 4000，auto=true，恒一行放行"""
    for label, filler in (("纯英文", "A"), ("中英混排", "字a")):
        engine.reset(phase="idle", recording_left=None, take_texts=[])
        message = filler * 5000
        code, out, err = run_hook(env, "stop",
                                  json.dumps({"last_assistant_message": message,
                                              "session_id": "s1"}))
        lines = out.decode("utf-8", "replace").splitlines()
        ck("stop(%s)：退出码 0" % label, code == 0, err.decode("utf-8", "replace"))
        ck("stop(%s)：stdout 恰好一行 {\"continue\": true}" % label,
           len(lines) == 1 and json.loads(lines[0]) == {"continue": True}, lines)
        speaks = [a for c, a in engine.seen if c == "speak"]
        ck("stop(%s)：speak 收到 4000 字符 + auto=true" % label,
           bool(speaks) and speaks[0].get("auto") is True
           and len(speaks[0].get("text", "")) == 4000, speaks)
        ck("stop(%s)：截断按字符（前 4000 个）" % label,
           bool(speaks) and speaks[0].get("text") == message[:4000], "")


def check_engine_absent(ck: Checker, client: MCPClient, engine: FakeEngine) -> None:
    """引擎停了：status 回计划表里写死的那句话；prompt_hook 放行"""
    engine.stop()
    time.sleep(0.2)
    ck("假引擎确实停了", not engine.alive, "")
    data = tool_json(call_tool(client, "voice_pill_status")["result"])
    ck("status（引擎不在）：running=false", data.get("running") is False, data)
    ck("status（引擎不在）：note 逐字对齐", data.get("note") == NOTE_NOT_RUNNING, data)
    data = tool_json(call_tool(client, "voice_pill_prompt_hook", {})["result"])
    ck("prompt_hook（引擎不在）：放行", data == {"continue": True}, data)


def check_hook_session_start(ck: Checker, env: dict, sandbox: str) -> None:
    """session-start 钩子：恒一行放行、写锚点纸条、不真拉看门狗"""
    code, out, err = run_hook(env, "session-start",
                              json.dumps({"session_id": "s1", "source": "startup"}))
    lines = out.decode("utf-8", "replace").splitlines()
    ck("session-start：退出码 0", code == 0, err.decode("utf-8", "replace"))
    ck("session-start：stdout 恰好一行 {\"continue\": true}",
       len(lines) == 1 and json.loads(lines[0]) == {"continue": True}, lines)

    anchor = os.path.join(sandbox, "VoicePill", "anchor.json")
    if ck("session-start：沙箱里出现锚点纸条 anchor.json", os.path.isfile(anchor), anchor):
        with open(anchor, encoding="utf-8") as fh:
            note = json.load(fh)
        ck("session-start：纸条记的是 ChatGPT.exe / codex.exe",
           os.path.basename(note.get("image", "")).lower() in ("chatgpt.exe", "codex.exe"),
           note)

    log = os.path.join(sandbox, "VoicePill", "plugin.log")
    text = ""
    if os.path.isfile(log):
        with open(log, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    ck("session-start：日志里有 supervise_missing（临时根拉不起引擎）",
       "supervise_missing" in text, text[-300:])
    ck("session-start：日志里没有 supervisor_spawned（没真起看门狗）",
       "supervisor_spawned" not in text, "")


def run_hook(env: dict, kind: str, payload_text: str, timeout: float = 30.0):
    """跑一次命令行钩子：stdin 喂完就关（钩子会读到 EOF 再答，没有 stdio 那个坑）。"""
    proc = subprocess.Popen([EXE, kind], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env)
    out, err = proc.communicate(payload_text.encode("utf-8"), timeout=timeout)
    return proc.returncode, out, err


def main() -> int:
    if not os.path.isfile(EXE):
        print("❌ 找不到 %s。先跑 tools\\build-plugin-exe.ps1。" % EXE)
        return 1
    with open(GOLD_PATH, encoding="utf-8") as fh:
        gold = json.load(fh)

    sandbox = tempfile.mkdtemp(prefix="vp-pluginexe-")
    pipe = r"\\.\pipe\VoicePill-Json-selftest-%d" % os.getpid()
    os.environ["LOCALAPPDATA"] = sandbox
    os.environ[bridge.JSON_PIPE_NAME_ENV] = pipe
    os.environ["VOICEPILL_ENGINE_ROOT"] = os.path.join(sandbox, "no-such-root")
    os.environ["VOICEPILL_SUPERVISOR_MUTEX"] = \
        r"Local\VoicePillTest-%d-%d" % (os.getpid(), int(time.time() * 1000))
    _require_test_pipe()
    env = dict(os.environ)

    ck = Checker()
    print("=== 插件 exe 端到端自测（假引擎 + 真 exe）===")
    print("exe   ：%s" % EXE)
    print("管道  ：%s" % pipe)
    print("沙箱  ：%s" % sandbox)

    engine = FakeEngine().start()
    ck("假引擎起来了", _wait(lambda: engine.started, 5.0), "error=%s" % engine.error)
    client = MCPClient(EXE, env)
    try:
        hello = client.handshake()
        info = (hello.get("result") or {}).get("serverInfo") or {}
        ck("initialize：serverInfo.name=voice_pill", info.get("name") == "voice_pill",
           hello)

        print("\n[%s]" % "tools/list 契约（对 gold 基线）")
        check_tools_list(ck, client, gold)
        dump_tools_evidence(client.request("tools/list", {})["result"]["tools"])

        print("\n[%s]" % "tools/call 八个工具")
        check_tools_call(ck, client, engine)

        print("\n[%s]" % "钩子 prompt-submit / stop")
        check_hooks_prompt_submit(ck, engine, env)
        check_hooks_stop(ck, engine, env)

        print("\n[%s]" % "引擎不在时的降级")
        check_engine_absent(ck, client, engine)

        print("\n[%s]" % "钩子 session-start（引擎不在，也不许真拉）")
        check_hook_session_start(ck, env, sandbox)
    finally:
        code, stderr = client.close()
        engine.stop()

    ck("exe 退出码 0 且 stderr 是空的",
       code == 0 and not stderr.strip(),
       "code=%r stderr=%r" % (code, stderr))
    ck("stdio 上没有解析不了的行", not client.parse_errors, client.parse_errors)

    print("\n%s（%d 项失败 / %d 项检查）"
          % ("✅ 全部通过" if not ck.failed else "❌ 有失败", ck.failed, ck.total))
    return 1 if ck.failed else 0


if __name__ == "__main__":
    sys.exit(main())
