#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""在本平台实测组装好的 Signal 运行时（runtime 工作流在 Windows、macOS 机器上各跑一次）。

用法：test_runtime.py <运行时目录> --classes <Smoke.class、SqliteSmoke.class 所在目录> [--no-link]

先查二进制：macOS 包里的 Mach-O 全部只有 arm64，原生库只依赖系统库；Windows 原生库依赖的 VC 运行库都在 jre/bin
（加载 native/ 里的 dll 时按 java.exe 所在目录找依赖，干净的 Windows 上也能加载）。
然后全部用包里的 jre/bin/java、classpath.txt 的顺序和 -Djava.library.path=native，与 NexaChat 启动 signal-cli 的方式一致：
  1. org.asamk.signal.Main --version 输出 signal-cli <版本>；
  2. Smoke（libsignal 生成一对身份密钥）：不给 java.library.path 必须失败，证明 jar 里没有原生库、不会解压到临时目录；
     给了必须成功，且 -Xlog:library 记下加载的正是 native/ 里那个文件；
  3. SqliteSmoke：sqlite-jdbc 能加载本平台的原生库（其他平台的已从 jar 里删掉）；
  4. 多账号 jsonRpc（不带 -a）下 startLink 拿到 sgnl://linkdevice 链接（不扫码；要能连上 Signal 服务器）。
"""

import argparse
import json
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time

NATIVE = {"win32-x64": "signal_jni.dll", "darwin-arm64": "libsignal_jni.dylib"}
JAVA = {"win32-x64": ["jre", "bin", "java.exe"], "darwin-arm64": ["jre", "bin", "java"]}


def fail(msg):
    sys.exit(f"[test] 失败: {msg}")


def run(cmd, timeout=120, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, **kw)
    return r.returncode, r.stdout, r.stderr


MACHO_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}


def check_macos_binaries(root, native):
    machos = []
    for dirpath, _, filenames in os.walk(root):
        for n in filenames:
            path = os.path.join(dirpath, n)
            if n.endswith((".jar", ".class")):
                continue
            with open(path, "rb") as f:
                if f.read(4) in MACHO_MAGIC:
                    machos.append(path)
    for path in machos:
        code, out, err = run(["lipo", "-archs", path])
        if code != 0 or out.split() != ["arm64"]:
            fail(f"{os.path.relpath(path, root)} 的架构不是只有 arm64: {out.strip()}{err.strip()}")
    print(f"[test] {len(machos)} 个 Mach-O 都只有 arm64")
    # otool -L 先列出 dylib 自己的安装名（编译机上的路径），要去掉
    install_name = run(["otool", "-D", native])[1].splitlines()[1:]
    code, out, err = run(["otool", "-L", native])
    deps = [d for d in (l.strip().split(" (")[0] for l in out.splitlines()[1:] if l.strip()) if d not in install_name]
    bad = [d for d in deps if not d.startswith(("/usr/lib/", "/System/Library/"))]
    if code != 0 or bad:
        fail(f"{native} 依赖了系统以外的库: {bad or err}")
    print(f"[test] {os.path.basename(native)} 只依赖系统库: {', '.join(os.path.basename(d) for d in deps)}")


def pe_imports(path):
    """PE 文件导入的 DLL 名（只读导入表）。"""
    with open(path, "rb") as f:
        data = f.read()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        fail(f"{path} 不是 PE 文件")
    sections_count = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    if struct.unpack_from("<H", data, opt)[0] != 0x20B:
        fail(f"{path} 不是 64 位 PE")
    import_rva = struct.unpack_from("<I", data, opt + 112 + 8)[0]  # 数据目录第 1 项
    sections = [struct.unpack_from("<8sIIII", data, opt + opt_size + 40 * i) for i in range(sections_count)]

    def offset(rva):
        for _, vsize, va, rawsize, rawptr in sections:
            if va <= rva < va + max(vsize, rawsize):
                return rva - va + rawptr
        fail(f"{path}: RVA {rva:#x} 不在任何节里")

    names = []
    pos = offset(import_rva)
    while True:
        name_rva = struct.unpack_from("<I", data, pos + 12)[0]
        if name_rva == 0:
            return names
        start = offset(name_rva)
        names.append(data[start:data.index(b"\0", start)].decode("ascii"))
        pos += 20


def check_windows_binaries(root, native):
    imports = pe_imports(native)
    print(f"[test] {os.path.basename(native)} 导入: {', '.join(imports)}")
    bin_dir = os.path.join(root, "jre", "bin")
    shipped = {n.lower() for n in os.listdir(bin_dir)}
    vc = [n for n in imports if re.fullmatch(r"(msvcp|vcruntime|concrt)\d+(_\d+)?\.dll|ucrtbase\.dll", n.lower())]
    if not vc:
        fail(f"{native} 应依赖 VC 运行库（MSVCP140/VCRUNTIME140），导入表解析可能有误")
    missing = [n for n in vc if n.lower() not in shipped]
    if missing:
        fail(f"jre/bin 里缺少原生库依赖的 VC 运行库: {missing}")
    print(f"[test] VC 运行库都在 jre/bin: {', '.join(vc)}")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("root")
    p.add_argument("--classes", required=True)
    p.add_argument("--no-link", action="store_true")
    a = p.parse_args()
    for stream in (sys.stdout, sys.stderr):  # Windows 机器的控制台默认不是 UTF-8
        stream.reconfigure(encoding="utf-8", errors="replace")
    root = os.path.realpath(a.root)
    classes = os.path.realpath(a.classes)  # 下面有的命令换了工作目录，类路径必须是绝对路径
    with open(os.path.join(root, "VERSION.json"), encoding="utf-8") as f:
        version = json.load(f)
    target = version["target"]
    java = os.path.join(root, *JAVA[target])
    native_dir = os.path.join(root, "native")
    native = os.path.join(native_dir, NATIVE[target])
    with open(os.path.join(root, "classpath.txt"), encoding="utf-8") as f:
        jars = [l.strip() for l in f if l.strip()]
    cp = os.pathsep.join(os.path.join(root, "lib", j) for j in jars)
    base = [java, "--enable-native-access=ALL-UNNAMED"]
    lib_path = f"-Djava.library.path={native_dir}"
    print(f"[test] {json.dumps(version)}; {len(jars)} jars", flush=True)
    if target == "darwin-arm64":
        check_macos_binaries(root, native)
    else:
        check_windows_binaries(root, native)

    code, out, err = run([java, "-version"])
    print(err.strip())
    if code != 0:
        fail("java -version")

    # 1. signal-cli --version
    code, out, err = run([*base, lib_path, "-cp", cp, "org.asamk.signal.Main", "--version"])
    print(f"[test] --version: {out.strip()}")
    if code != 0 or out.strip() != f"signal-cli {version['signalCli']}":
        fail(f"signal-cli --version（exit {code}）: {out}{err}")

    # 2. libsignal 只能从 java.library.path 加载
    smoke_cp = os.pathsep.join([classes, cp])
    code, out, err = run([*base, "-cp", smoke_cp, "Smoke"])
    if code == 0 or "signal_jni" not in out + err:
        fail(f"不给 java.library.path 时 libsignal 不应能加载（exit {code}）: {out}{err}")
    print("[test] 不给 java.library.path：libsignal 加载失败（符合预期，jar 里没有原生库）")
    tmp = tempfile.mkdtemp(prefix="signal-runtime-test-")
    try:
        # -Xlog 的 file= 不能带盘符里的冒号，所以在临时目录里运行、写相对路径
        code, out, err = run([*base, lib_path, "-Xlog:library=info:file=library.log", "-cp", smoke_cp, "Smoke"], cwd=tmp)
        if code != 0:
            fail(f"Smoke（exit {code}）: {out}{err}")
        print(f"[test] {out.strip()}")
        with open(os.path.join(tmp, "library.log"), encoding="utf-8", errors="replace") as f:
            loaded = [l.strip() for l in f if "Loaded library" in l and "signal_jni" in l]
        print("[test] -Xlog:library: " + " | ".join(loaded))
        if len(loaded) != 1 or os.path.normcase(f"Loaded library {native},") not in os.path.normcase(loaded[0]):
            fail(f"-Xlog:library 里应只有一条从 {native} 加载 libsignal 的记录")

        # 3. sqlite-jdbc
        code, out, err = run([*base, f"-Djava.io.tmpdir={tmp}", "-cp", smoke_cp, "SqliteSmoke"])
        if code != 0:
            fail(f"SqliteSmoke（exit {code}）: {out}{err}")
        print(f"[test] {out.strip()}")

        # 4. 多账号 jsonRpc 下 startLink
        if not a.no_link:
            start_link(base, lib_path, cp, os.path.join(tmp, "data"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("[test] 全部通过")


def start_link(base, lib_path, cp, data_dir):
    cmd = [*base, lib_path, "-cp", cp, "org.asamk.signal.Main", "--data-dir", data_dir, "jsonRpc"]
    started = time.monotonic()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")
    lines = queue.Queue()
    stderr = []
    threading.Thread(target=lambda: [lines.put(l) for l in proc.stdout], daemon=True).start()
    threading.Thread(target=lambda: [stderr.append(l) for l in proc.stderr], daemon=True).start()
    try:
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": "link-1", "method": "startLink"}) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + 90
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                fail("90 秒内没有拿到 startLink 的结果\n" + "".join(stderr[-30:]))
            try:
                line = lines.get(timeout=min(left, 1))
            except queue.Empty:
                if proc.poll() is not None:
                    fail(f"signal-cli 退出（exit {proc.returncode}）\n" + "".join(stderr[-30:]))
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") != "link-1":
                continue
            if "error" in msg:
                fail(f"startLink 返回错误: {json.dumps(msg['error'], ensure_ascii=False)}")
            uri = (msg.get("result") or {}).get("deviceLinkUri", "")
            if not uri.startswith("sgnl://linkdevice?"):
                fail(f"startLink 的结果不是 sgnl://linkdevice 链接: {line.strip()}")
            print(f"[test] startLink: {uri.split('?')[0]}?…（{time.monotonic() - started:.1f} 秒）")
            return
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


if __name__ == "__main__":
    main()
