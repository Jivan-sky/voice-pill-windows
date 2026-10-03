# -*- coding: utf-8 -*-
"""开机自启 / 取消自启 / 查看状态。

为什么首选「计划任务」而不是启动文件夹
------------------------------------
驻留型工具最坏的失败模式是「崩了之后就再也不在」，而用户不会每天去检查
它还活着。计划任务自带**失败后重启**（RestartCount / RestartInterval），
这正是启动文件夹快捷方式和注册表 Run 项都给不了的；而且任务计划里那条
记录看得见、删得掉，比注册表透明。

所以：先试计划任务；注册不了（权限、系统策略、旧系统缺 cmdlet）再退到
启动文件夹快捷方式，并把「你因此没有崩溃自拉」说清楚，不装作一回事。

实测本机（2026-10-03）**非管理员注册不了任何计划任务**：
`Register-ScheduledTask` 与 `schtasks /create /sc onlogon` 四种写法全部
Access is denied。所以这里实际会走启动文件夹那条路——那时崩溃自拉由
`supervise.py` 顶上（挂它而不是直接挂 main.py），两条路的最终可靠性一致。

自启挂的是看门狗，不是真身
------------------------
`src/supervise.py` 负责拉起 `src/main.py` 并在它非正常退出时重拉。对计划任务
那条路这就是多一层（任务的重启策略本来也能干），但让两条路**装出来是同一个
东西**更重要：不然"我明明装了自启，怎么不拉"会因为走哪条路而不同，
这种不确定最难查。

为什么用 pythonw
---------------
`python.exe` 是控制台子系统程序，开机自启会弹一个黑窗口，关掉窗口就等于
杀掉进程。`pythonw.exe` 不弹窗，代价是 stdout/stderr 为 None——这由
console.attach_app_log() 兜住，输出进 `%LOCALAPPDATA%\\VoicePill\\logs\\app.log`。

关于"粘不进管理员窗口"
--------------------
普通权限运行时，Windows 的 UIPI 会拦下向高完整性（管理员）窗口注入的
Ctrl+V。任务计划可以设「以最高权限运行」解决，但那需要管理员权限去注册，
本模块**不擅自提权**——需要的话按 README 里的说明手工勾一下即可。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

import config

TASK_NAME = "VoicePill"
SHORTCUT_NAME = "VoicePill.lnk"


def pythonw_path() -> str:
    """同目录下的 pythonw.exe（uv/标准 venv 都有）；没有就退回当前解释器。"""
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return exe if os.path.isfile(exe) else sys.executable


def entry_path() -> str:
    """真身：干活的进程。"""
    return os.path.join(config.project_root(), "src", "main.py")


def guard_path() -> str:
    """自启实际拉起的进程：看门狗（见 supervise.py）。"""
    return os.path.join(config.project_root(), "src", "supervise.py")


def plan() -> dict:
    """把"要装成什么"摊开给用户看，不产生任何副作用。"""
    return {
        "exe": pythonw_path(),
        "arg": '"%s"' % guard_path(),
        "cwd": config.project_root(),
        "task": TASK_NAME,
        "shortcut": _startup_shortcut(),
    }


def describe() -> str:
    p = plan()
    return ("可执行文件：%s\n  参数      ：%s\n  工作目录  ：%s\n"
            "  看门狗    ：%s（子进程非正常退出时重拉，短时连崩 3 次则放弃）\n"
            "  计划任务  ：%s（失败后自动重启 3 次，每次间隔 1 分钟）\n"
            "  备用位置  ：%s" % (p["exe"], p["arg"], p["cwd"],
                                  guard_path(), p["task"], p["shortcut"]))


def _startup_shortcut() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Microsoft", "Windows", "Start Menu",
                        "Programs", "Startup", SHORTCUT_NAME)


def _ps_literal(text: str) -> str:
    """塞进 PowerShell 单引号字符串。单引号本身按 PS 规则写两遍。"""
    return "'" + text.replace("'", "''") + "'"


def _run_powershell(script: str) -> tuple:
    """跑一段一次性脚本。用文件而不是 -Command：路径里的 & 和引号走命令行
    会被二次解析，这是本工程已经踩过的坑（见 docs/移植方案.md 6.4）。"""
    with tempfile.TemporaryDirectory(prefix="voicepill-task-") as tmp:
        path = os.path.join(tmp, "task.ps1")
        # 必须带 BOM：PowerShell 5.1 读无 BOM 的 .ps1 会按 ANSI(GBK) 解释，
        # 脚本里的中文注释和输出会变乱码。
        with open(path, "w", encoding="utf-8-sig") as fh:
            fh.write(script)
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", path],
            capture_output=True,
        )
    text = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
    return proc.returncode, text


_PS_PRELUDE = """$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
"""


def install() -> int:
    p = plan()
    script = _PS_PRELUDE + """
try {
  $action = New-ScheduledTaskAction -Execute %s -Argument %s -WorkingDirectory %s
  $trigger = New-ScheduledTaskTrigger -AtLogOn
  $sets = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
      -DontStopIfGoingOnBatteries -StartWhenAvailable `
      -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
      -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
  Register-ScheduledTask -TaskName %s -Action $action -Trigger $trigger `
      -Settings $sets -Force `
      -Description '按住 Fn 说话的语音输入；日志在 %%LOCALAPPDATA%%\\VoicePill\\logs\\app.log' | Out-Null
  Write-Output 'OK 计划任务已注册（登录时自动启动，失败后自动重启）'
} catch {
  Write-Output ('FAIL ' + $_.Exception.Message)
  exit 1
}
""" % (_ps_literal(p["exe"]), _ps_literal(p["arg"]), _ps_literal(p["cwd"]),
       _ps_literal(p["task"]))

    code, out = _run_powershell(script)
    print(out)
    if code == 0:
        return 0

    print("\n计划任务没注册上，改用启动文件夹快捷方式。")
    return _install_shortcut(p)


def _install_shortcut(p: dict) -> int:
    script = _PS_PRELUDE + """
try {
  $ws = New-Object -ComObject WScript.Shell
  $lnk = $ws.CreateShortcut(%s)
  $lnk.TargetPath = %s
  $lnk.Arguments = %s
  $lnk.WorkingDirectory = %s
  $lnk.Description = 'VoicePill'
  $lnk.Save()
  Write-Output 'OK 启动文件夹快捷方式已创建'
} catch {
  Write-Output ('FAIL ' + $_.Exception.Message)
  exit 1
}
""" % (_ps_literal(p["shortcut"]), _ps_literal(p["exe"]),
       _ps_literal(p["arg"]), _ps_literal(p["cwd"]))

    code, out = _run_powershell(script)
    print(out)
    if code == 0:
        print("说明：启动文件夹没有「失败后重启」策略，所以快捷方式挂的是\n"
              "      src/supervise.py（看门狗），崩溃自拉由它提供。\n"
              "      想换成计划任务（多一层系统级重启），用管理员 PowerShell\n"
              "      再跑一次 `python main.py --install-autostart`。")
    return code


def uninstall() -> int:
    p = plan()
    script = _PS_PRELUDE + """
try {
  Unregister-ScheduledTask -TaskName %s -Confirm:$false -ErrorAction Stop
  Write-Output 'OK 计划任务已删除'
} catch {
  Write-Output ('SKIP 计划任务：' + $_.Exception.Message)
}
if (Test-Path -LiteralPath %s) {
  Remove-Item -LiteralPath %s -Force
  Write-Output 'OK 启动文件夹快捷方式已删除'
} else {
  Write-Output 'SKIP 启动文件夹里没有快捷方式'
}
exit 0
""" % (_ps_literal(p["task"]), _ps_literal(p["shortcut"]),
       _ps_literal(p["shortcut"]))

    code, out = _run_powershell(script)
    print(out)
    return code


def status() -> str:
    """给自检用的一句话状态。"""
    script = _PS_PRELUDE + """
$t = Get-ScheduledTask -TaskName %s -ErrorAction SilentlyContinue
if ($t) { Write-Output ('TASK ' + $t.State) } else { Write-Output 'NONE' }
""" % _ps_literal(TASK_NAME)
    _, out = _run_powershell(script)
    line = out.strip().splitlines()[-1] if out.strip() else "NONE"
    shortcut = "有" if os.path.isfile(_startup_shortcut()) else "无"
    if line.startswith("TASK"):
        return "计划任务（%s）+ 启动文件夹快捷方式：%s" % (line[5:], shortcut)
    return "未注册计划任务；启动文件夹快捷方式：%s" % shortcut
