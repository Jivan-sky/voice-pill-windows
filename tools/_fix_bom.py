# -*- coding: utf-8 -*-
"""把指定文件改写成带 BOM 的 UTF-8。

PowerShell 5.1 读 .ps1 时不认无 BOM 的 UTF-8，会退回系统 ANSI 代码页（本机
GBK），中文注释被按字节拆开，直接把语法搞坏（报 `UnexpectedToken`）。
写成 `utf-8-sig` 它才认得。

用法： python tools/_fix_bom.py <文件> [<文件> ...]
"""
import sys

for path in sys.argv[1:]:
    raw = open(path, "rb").read()
    if raw.startswith(b"\xef\xbb\xbf"):
        print("跳过（已有 BOM）：%s" % path)
        continue
    text = raw.decode("utf-8")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(text)
    back = open(path, "rb").read()
    assert back.startswith(b"\xef\xbb\xbf"), "BOM 没写上"
    back.decode("utf-8")                      # 自校验：合法 UTF-8
    print("OK  已加 BOM：%s  %d → %d 字节" % (path, len(raw), len(back)))
