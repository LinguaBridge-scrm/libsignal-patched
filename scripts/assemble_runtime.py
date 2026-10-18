#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""组装 NexaChat 用的 Signal 运行时包：signal-cli + jlink 裁剪的 Temurin JRE + 补丁版 libsignal 原生库。

用法：
  assemble_runtime.py --target <win32-x64|darwin-arm64> --version <包版本> --out <目录> --tar <tar.gz>
      --jdk <JDK 目录> --jdk-url <下载地址> --jdk-sha256 <hex> --jdk-source-url <源码包地址>
      --signal-cli <signal-cli-<版本>.tar.gz> --patched-jar <本平台补丁版 libsignal-client jar>
      --native <补丁版原生库> --patch <补丁文件> --patch-commit <本仓库提交> --natives-run <编译原生库的运行地址>
      --libsignal-version <版本> --libsignal-commit <提交> --signal-cli-commit <提交>
      --license-dir <目录：signal-cli-LICENSE、libsignal-LICENSE、libsignal-acknowledgments.md> [--built-by <地址>]

包内布局（NexaChat 主机按它找文件，不要改）：
  VERSION.json    {signalCli, libsignal, patchCommit, jre, target}
  jre/            jlink 裁剪的 Temurin（符号链接一律换成文件副本）
  lib/            signal-cli 发行包的全部 jar；libsignal-client 换成补丁版并删掉 jar 里所有原生库，
                  sqlite-jdbc 只保留本平台的原生库
  native/         本平台的补丁版 libsignal 原生库（signal_jni.dll / libsignal_jni.dylib），经 java.library.path 加载
  classpath.txt   signal-cli 自带 bin/signal-cli 里 CLASSPATH 的顺序，一行一个 jar 文件名
  licenses/       signal-cli、libsignal、Temurin 的许可证和 SOURCES.md

tar.gz 可复现：条目按路径排序，时间戳取 SOURCE_DATE_EPOCH（缺省为 0），属主为 0，gzip 头不带文件名和时间。
"""

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile

# 技术验证用过的模块（signal-cli 0.14.9 的 jdeps 结果加上 jdk.crypto.ec、jdk.localedata 等运行时才用到的）
MODULES = [
    "java.base", "java.desktop", "java.instrument", "java.management", "java.sql", "java.naming", "java.xml",
    "java.logging", "jdk.httpserver", "jdk.net", "jdk.security.auth", "jdk.unsupported", "jdk.crypto.ec",
    "jdk.zipfs", "jdk.localedata",
]
# 与技术验证时的 jre-min 相同：只留英文、中文的区域数据，模块资源用 zip 压缩（不压缩时 JRE 约 114 MB，压缩后约 53 MB）
JLINK_FLAGS = ["--include-locales=en,zh", "--strip-debug", "--no-man-pages", "--no-header-files", "--compress=zip-6"]

TARGETS = {
    "win32-x64": {
        "native": "signal_jni.dll",  # System.mapLibraryName("signal_jni")
        "built": "signal_jni_amd64.dll",  # 官方 jar 里的文件名（build_jni.sh）
        "sqlite": "org/sqlite/native/Windows/x86_64/",
        "java": "jre/bin/java.exe",
    },
    "darwin-arm64": {
        "native": "libsignal_jni.dylib",
        "built": "libsignal_jni_aarch64.dylib",
        "sqlite": "org/sqlite/native/Mac/aarch64/",
        "java": "jre/bin/java",
    },
}
TOP_LEVEL = ["VERSION.json", "classpath.txt", "jre", "lib", "licenses", "native"]


def die(msg):
    sys.exit(f"[assemble] {msg}")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_native(name):
    return "/" not in name and name.endswith((".dll", ".dylib", ".so"))


def rewrite_jar(data, out_path, keep):
    """按原顺序照搬 keep(name) 为真的条目（时间戳、压缩方式、属性不变），返回删掉的条目名。"""
    dropped = []
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out_path, "w") as dst:
        for info in src.infolist():
            if not keep(info.filename):
                dropped.append(info.filename)
                continue
            # 写入时 zipfile 会把为 0 的外部属性改成 0o600，写完再还原（中央目录在关闭时才写）
            attr = info.external_attr
            dst.writestr(info, src.read(info))
            info.external_attr = attr
    return dropped


def java_entries(data):
    """jar 里除原生库以外的条目：{名字: CRC}。"""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {i.filename: i.CRC for i in z.infolist() if not is_native(i.filename)}


def read_signal_cli(tar_path, version):
    """返回 (CLASSPATH 里的 jar 名列表, {jar 名: 内容})。"""
    root = f"signal-cli-{version}/"
    jars = {}
    script = None
    with tarfile.open(tar_path, "r:gz") as tar:
        for m in tar.getmembers():
            if not m.isfile():
                continue
            if m.name == root + "bin/signal-cli":
                script = tar.extractfile(m).read().decode("utf-8")
            elif m.name.startswith(root + "lib/") and m.name.endswith(".jar") and m.name.count("/") == 2:
                jars[m.name[len(root + "lib/"):]] = tar.extractfile(m).read()
    if script is None:
        die(f"{tar_path} 里没有 {root}bin/signal-cli")
    lines = [l for l in script.splitlines() if l.startswith("CLASSPATH=")]
    if len(lines) != 1:
        die(f"bin/signal-cli 里应有且只有一行 CLASSPATH=，实际 {len(lines)} 行")
    order = []
    for part in lines[0][len("CLASSPATH="):].split(":"):
        if not part.startswith("$APP_HOME/lib/"):
            die(f"CLASSPATH 里有意外的条目：{part}")
        order.append(part[len("$APP_HOME/lib/"):])
    if len(set(order)) != len(order):
        die("CLASSPATH 里有重复的 jar")
    if set(order) != set(jars):
        die(f"CLASSPATH 与 lib/ 不一致：缺 {sorted(set(order) - set(jars))}，多 {sorted(set(jars) - set(order))}")
    return order, jars


def dereference_symlinks(root):
    """jlink 在 macOS 上把 legal/ 里重复的许可证做成符号链接；包里只放普通文件和目录。"""
    links = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            if os.path.islink(p):
                links.append(p)
    for p in links:
        target = os.path.realpath(p)
        if not os.path.isfile(target) or os.path.commonpath([os.path.realpath(root), target]) != os.path.realpath(root):
            die(f"符号链接指向包外或不是文件：{p} -> {os.readlink(p)}")
        os.unlink(p)
        shutil.copy2(target, p)
    return len(links)


def jlink(jdk, out_dir):
    exe = os.path.join(jdk, "bin", "jlink.exe" if os.name == "nt" else "jlink")
    cmd = [exe, "--add-modules", ",".join(MODULES), *JLINK_FLAGS, "--output", out_dir]
    print("[assemble] " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def read_release(path):
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if "=" in line:
                k, v = line.rstrip("\n").split("=", 1)
                out[k] = v.strip('"')
    return out


def sources_md(a, info):
    t = TARGETS[a.target]
    patch_url = f"https://github.com/LinguaBridge-scrm/libsignal-patched/blob/{a.patch_commit}/{a.patch_path}"
    lines = [
        f"# Signal runtime {a.version} ({a.target}): components and corresponding source",
        "",
        "This package lets NexaChat run signal-cli as a local process. It bundles three components, each under its",
        "own licence. The licence texts are in this folder; NexaChat itself is not covered by them.",
        "",
        f"Assembled by {a.built_by or 'a local run'} with scripts/assemble_runtime.py of",
        "https://github.com/LinguaBridge-scrm/libsignal-patched.",
        "",
        "| Component | Exact version | Licence | Location in this package |",
        "|---|---|---|---|",
        f"| signal-cli | {info['signalCli']} | GPL-3.0 (`signal-cli-LICENSE.txt`) | `lib/`, `classpath.txt` |",
        f"| libsignal, patched | {info['libsignal']} + network patch | AGPL-3.0-only (`libsignal-LICENSE.txt`; Rust dependencies: "
        f"`libsignal-acknowledgments.md`) | `native/{t['native']}`, `lib/libsignal-client-{info['libsignal']}.jar` |",
        f"| Eclipse Temurin JRE | {info['jre']} | GPL-2.0 with Classpath Exception (`temurin-LICENSE.txt`, "
        f"`temurin-ASSEMBLY_EXCEPTION.txt`, `temurin-ADDITIONAL_LICENSE_INFO.txt`, `temurin-NOTICE.txt`; per-module notices "
        f"in `jre/legal/`) | `jre/` |",
        "",
        f"## signal-cli {info['signalCli']}",
        "",
        f"- Source: https://github.com/AsamK/signal-cli/tree/v{info['signalCli']} (commit {a.signal_cli_commit}).",
        f"- Binary release used: https://github.com/AsamK/signal-cli/releases/download/v{info['signalCli']}/"
        f"signal-cli-{info['signalCli']}.tar.gz (SHA-256 {info['signalCliSha256']}).",
        "- signal-cli itself is unchanged. `lib/` holds every jar of that release, byte for byte, except:",
        f"  - `libsignal-client-{info['libsignal']}.jar`: the patched build below with all native libraries removed; "
        f"libsignal loads `native/{t['native']}` through `java.library.path` instead;",
        f"  - `{info['sqliteJar']}`: native libraries of other platforms removed (only `{t['sqlite']}` is kept).",
        "- The other jars are third-party libraries that signal-cli distributes under their own licences; see the",
        "  `META-INF` folder of each jar. `classpath.txt` lists the jars in the order of the release's `bin/signal-cli`.",
        "",
        f"## libsignal {info['libsignal']} (patched)",
        "",
        f"- Upstream source: https://github.com/signalapp/libsignal/tree/v{info['libsignal']} (commit {a.libsignal_commit}).",
        f"- Patch: {patch_url} (SHA-256 {info['patchSha256']}). It changes only the network layer: a configured proxy",
        "  is never bypassed (`ProxyOnly` instead of `ProxyThenDirect`), and SOCKS5 host names are resolved by the proxy.",
        f"- Native library built from commit {a.patch_commit} of LinguaBridge-scrm/libsignal-patched with the upstream",
        f"  `java/build_jni.sh desktop`: {a.natives_run}",
        f"- `native/{t['native']}` (built as `{t['built']}`): SHA-256 {info['nativeSha256']}.",
        "- `libsignal-acknowledgments.md` is upstream's licence listing of the Rust dependencies of libsignal-jni",
        f"  (`acknowledgments/acknowledgments-android.md` at v{info['libsignal']}, the same crate as this desktop build).",
        "",
        f"## Eclipse Temurin {info['jre']}",
        "",
        f"- Binary: {a.jdk_url} (SHA-256 {a.jdk_sha256}), reduced with jlink:",
        f"  `--add-modules {','.join(MODULES)} {' '.join(JLINK_FLAGS)}`.",
        f"- Source: {a.jdk_source_url}",
        f"  (repository {info['jreSourceRepo']}, revision `{info['jreSourceRev']}` as recorded in the JDK's `release` file).",
        "",
    ]
    return "\n".join(lines)


def make_tar(src_dir, tar_path, target):
    """可复现的 ustar + gzip。Windows 包里所有文件都是 0644；macOS 包保留可执行位。"""
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    entries = []
    for dirpath, dirnames, filenames in os.walk(src_dir):
        dirnames.sort()
        rel = os.path.relpath(dirpath, src_dir).replace(os.sep, "/")
        if rel != ".":
            entries.append((rel, True))
        for n in sorted(filenames):
            entries.append(((n if rel == "." else f"{rel}/{n}"), False))
    entries.sort(key=lambda e: e[0])
    with open(tar_path, "wb") as out, \
            gzip.GzipFile(filename="", mode="wb", fileobj=out, compresslevel=9, mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for rel, is_dir in entries:
            path = os.path.join(src_dir, *rel.split("/"))
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                die(f"包里不能有符号链接：{rel}")
            ti = tarfile.TarInfo(rel + ("/" if is_dir else ""))
            ti.mtime = epoch
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            if is_dir:
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
                tar.addfile(ti)
            else:
                ti.size = st.st_size
                executable = target != "win32-x64" and bool(st.st_mode & stat.S_IXUSR)
                ti.mode = 0o755 if executable else 0o644
                with open(path, "rb") as f:
                    tar.addfile(ti, f)


def dir_bytes(path):
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for n in filenames:
            total += os.lstat(os.path.join(dirpath, n)).st_size
    return total


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    for name in ["target", "version", "out", "tar", "jdk", "jdk-url", "jdk-sha256", "jdk-source-url", "signal-cli",
                 "patched-jar", "native", "patch", "patch-commit", "natives-run", "libsignal-version", "libsignal-commit",
                 "signal-cli-commit", "license-dir"]:
        p.add_argument(f"--{name}", required=True)
    p.add_argument("--patch-path", default="patches/libsignal-v0.103.0.patch", help="补丁在本仓库里的路径（写进 SOURCES.md）")
    p.add_argument("--built-by", default="", help="组装这次运行时的工作流地址（写进 SOURCES.md）")
    a = p.parse_args()
    if a.target not in TARGETS:
        die(f"--target 只能是 {', '.join(TARGETS)}")
    t = TARGETS[a.target]
    m = re.fullmatch(r"signal-cli-(\d+\.\d+\.\d+)\.tar\.gz", os.path.basename(a.signal_cli))
    if not m:
        die("--signal-cli 应为 signal-cli-<版本>.tar.gz")
    signal_cli_version = m.group(1)
    if not a.version.startswith(f"{signal_cli_version}-"):
        die(f"包版本 {a.version} 应以 signal-cli 版本 {signal_cli_version}- 开头")
    if os.path.exists(a.out) and os.listdir(a.out):
        die(f"输出目录非空：{a.out}")
    os.makedirs(a.out, exist_ok=True)
    out = a.out

    # lib/ 与 classpath.txt
    order, jars = read_signal_cli(a.signal_cli, signal_cli_version)
    lib_dir = os.path.join(out, "lib")
    os.makedirs(lib_dir)
    libsignal_jar = f"libsignal-client-{a.libsignal_version}.jar"
    if libsignal_jar not in jars:
        die(f"signal-cli {signal_cli_version} 的 lib/ 里没有 {libsignal_jar}")
    with open(a.patched_jar, "rb") as f:
        patched = f.read()
    if java_entries(patched) != java_entries(jars[libsignal_jar]):
        die(f"{a.patched_jar} 的类文件与 signal-cli 自带的 {libsignal_jar} 不同，不是同一版本的补丁版")
    native_sha = sha256_file(a.native)
    with zipfile.ZipFile(io.BytesIO(patched)) as z:
        natives_in_jar = {i.filename: hashlib.sha256(z.read(i)).hexdigest() for i in z.infolist() if is_native(i.filename)}
    if natives_in_jar.get(t["built"]) != native_sha:
        die(f"--native 与补丁版 jar 里的 {t['built']} 不一致（jar 里：{sorted(natives_in_jar)}），两者应来自同一次编译")
    sqlite_jars = [n for n in order if n.startswith("sqlite-jdbc-")]
    if len(sqlite_jars) != 1:
        die(f"应有且只有一个 sqlite-jdbc jar：{sqlite_jars}")
    for name in order:
        dst = os.path.join(lib_dir, name)
        if name == libsignal_jar:
            dropped = rewrite_jar(patched, dst, lambda n: not is_native(n))
            print(f"[assemble] lib/{name}: 补丁版，删掉原生库 {', '.join(dropped)}")
        elif name == sqlite_jars[0]:
            prefix = "org/sqlite/native/"
            keep_dirs = {prefix + "/".join(t["sqlite"][len(prefix):].split("/")[:i]) + "/" for i in range(1, 3)}

            def keep(n):
                return not n.startswith(prefix) or n == prefix or n.startswith(t["sqlite"]) or n in keep_dirs

            dropped = rewrite_jar(jars[name], dst, keep)
            with zipfile.ZipFile(dst) as z:
                kept = [i.filename for i in z.infolist() if i.filename.startswith(t["sqlite"]) and not i.filename.endswith("/")]
            if not kept:
                die(f"{name} 里没有 {t['sqlite']} 的原生库")
            print(f"[assemble] lib/{name}: 只保留 {', '.join(kept)}（删掉 {sum(not n.endswith('/') for n in dropped)} 个其他平台的原生库）")
        else:
            with open(dst, "wb") as f:
                f.write(jars[name])
    with open(os.path.join(out, "classpath.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(order) + "\n")

    # native/
    native_dir = os.path.join(out, "native")
    os.makedirs(native_dir)
    shutil.copyfile(a.native, os.path.join(native_dir, t["native"]))
    os.chmod(os.path.join(native_dir, t["native"]), 0o755)

    # jre/
    jre = os.path.join(out, "jre")
    jlink(a.jdk, jre)
    links = dereference_symlinks(jre)
    if links:
        print(f"[assemble] jre/: {links} 个符号链接已换成文件副本")
    if not os.path.isfile(os.path.join(out, *t["java"].split("/"))):
        die(f"jlink 产物里没有 {t['java']}")
    # jlink 产物的 release 只有 JAVA_VERSION 和 MODULES；完整版本号与源码信息取自 JDK 的 release
    release = read_release(os.path.join(jre, "release"))
    jdk_release = read_release(os.path.join(a.jdk, "release"))
    jre_version = jdk_release.get("SEMANTIC_VERSION") or ""
    if jdk_release.get("IMPLEMENTOR") != "Eclipse Adoptium" or not jre_version:
        die(f"--jdk 应为 Eclipse Temurin：IMPLEMENTOR={jdk_release.get('IMPLEMENTOR')}，SEMANTIC_VERSION={jre_version}")
    if release.get("JAVA_VERSION") != jdk_release.get("JAVA_VERSION"):
        die(f"jlink 产物的 JAVA_VERSION={release.get('JAVA_VERSION')} 与 JDK 的 {jdk_release.get('JAVA_VERSION')} 不一致")
    modules = set(release.get("MODULES", "").split())
    if not set(MODULES) <= modules:
        die(f"jlink 产物缺少模块：{sorted(set(MODULES) - modules)}")

    # licenses/
    lic = os.path.join(out, "licenses")
    os.makedirs(lic)
    copies = {
        "signal-cli-LICENSE.txt": os.path.join(a.license_dir, "signal-cli-LICENSE"),
        "libsignal-LICENSE.txt": os.path.join(a.license_dir, "libsignal-LICENSE"),
        "libsignal-acknowledgments.md": os.path.join(a.license_dir, "libsignal-acknowledgments.md"),
        "temurin-LICENSE.txt": os.path.join(jre, "legal", "java.base", "LICENSE"),
        "temurin-ASSEMBLY_EXCEPTION.txt": os.path.join(jre, "legal", "java.base", "ASSEMBLY_EXCEPTION"),
        "temurin-ADDITIONAL_LICENSE_INFO.txt": os.path.join(jre, "legal", "java.base", "ADDITIONAL_LICENSE_INFO"),
        "temurin-NOTICE.txt": os.path.join(a.jdk, "NOTICE"),
    }
    for name, src in copies.items():
        if not os.path.isfile(src):
            die(f"缺少许可证文件：{src}")
        shutil.copyfile(src, os.path.join(lic, name))
    info = {
        "signalCli": signal_cli_version,
        "signalCliSha256": sha256_file(a.signal_cli),
        "libsignal": a.libsignal_version,
        "patchSha256": sha256_file(a.patch),
        "nativeSha256": native_sha,
        "sqliteJar": sqlite_jars[0],
        "jre": jre_version,
        "jreSourceRepo": jdk_release.get("SOURCE_REPO", "").removesuffix(".git") or "unknown",
        "jreSourceRev": jdk_release.get("SOURCE", "") or "unknown",
    }
    with open(os.path.join(lic, "SOURCES.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(sources_md(a, info))

    # VERSION.json（字段固定）
    version = {"signalCli": signal_cli_version, "libsignal": a.libsignal_version, "patchCommit": a.patch_commit,
               "jre": jre_version, "target": a.target}
    with open(os.path.join(out, "VERSION.json"), "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(version, indent=2) + "\n")

    if sorted(os.listdir(out)) != TOP_LEVEL:
        die(f"包的顶层应为 {TOP_LEVEL}，实际 {sorted(os.listdir(out))}")
    sizes = {k: dir_bytes(os.path.join(out, k)) for k in ["jre", "lib", "native", "licenses"]}
    make_tar(out, a.tar, a.target)
    sizes["tar.gz"] = os.path.getsize(a.tar)
    print(f"[assemble] VERSION.json: {json.dumps(version)}")
    print("[assemble] 大小（字节）: " + ", ".join(f"{k}={v}" for k, v in sizes.items()))
    print(f"[assemble] {os.path.basename(a.tar)} SHA-256 {sha256_file(a.tar)}")


if __name__ == "__main__":
    main()
