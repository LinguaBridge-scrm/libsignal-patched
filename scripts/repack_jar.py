#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""把补丁版 libsignal 原生库换进 signal-cli 自带的 libsignal-client jar。

用法：repack_jar.py <原版 jar> <原生库目录> <输出目录>

输出目录里：
  libsignal-client-<版本>.jar          三个平台的补丁版原生库，直接覆盖 signal-cli 的 lib/ 里的同名文件
  <平台>/libsignal-client-<版本>.jar   只带这一个平台原生库的精简版，同样直接覆盖

原版 jar 里没有对应补丁版的原生库（macOS x86_64 的 libsignal_jni_amd64.dylib）一律删掉：
宁可在那个平台上加载失败，也不要悄悄用回会回退直连的原版。
其余条目（类文件、清单）原样照搬，时间戳和压缩方式不变。
"""

import os
import shutil
import sys
import zipfile

# 平台 → 官方 jar 里的原生库文件名（java/build_jni.sh 的 copy_built_library 命名）
PLATFORMS = {
    "windows-x64": "signal_jni_amd64.dll",
    "macos-arm64": "libsignal_jni_aarch64.dylib",
    "linux-x64": "libsignal_jni_amd64.so",
}


def is_native(name):
    return "/" not in name and name.endswith((".dll", ".dylib", ".so"))


def repack(original, out_path, natives):
    """natives：{jar 里的文件名: 本地文件}。原版 jar 里不在 natives 中的原生库都删掉。"""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    replaced = set()
    with zipfile.ZipFile(original) as src, zipfile.ZipFile(out_path, "w") as dst:
        for info in src.infolist():
            # 写入时 zipfile 会把为 0 的外部属性改成 0o600，写完再还原（中央目录在关闭时才写）
            attr = info.external_attr
            if not is_native(info.filename):
                dst.writestr(info, src.read(info))
                info.external_attr = attr
                continue
            path = natives.get(info.filename)
            if path is None:
                continue
            entry = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = info.create_system
            entry.file_size = os.path.getsize(path)
            with open(path, "rb") as f, dst.open(entry, "w") as w:
                shutil.copyfileobj(f, w, 1 << 20)
            entry.external_attr = attr
            replaced.add(info.filename)
    missing = sorted(set(natives) - replaced)
    if missing:
        sys.exit(f"原版 jar 里没有这些原生库，文件名可能不对：{', '.join(missing)}")


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    original, native_dir, out_dir = sys.argv[1:]
    jar_name = os.path.basename(original)

    natives = {}
    for platform, name in PLATFORMS.items():
        path = os.path.join(native_dir, name)
        if not os.path.isfile(path):
            sys.exit(f"缺少 {platform} 的原生库：{path}")
        natives[name] = path

    repack(original, os.path.join(out_dir, jar_name), natives)
    for platform, name in PLATFORMS.items():
        repack(original, os.path.join(out_dir, platform, jar_name), {name: natives[name]})

    # 列出每个 jar 里的原生库，日志里一眼能核对
    for platform in [None, *PLATFORMS]:
        rel = os.path.join(platform, jar_name) if platform else jar_name
        with zipfile.ZipFile(os.path.join(out_dir, rel)) as z:
            libs = [i.filename for i in z.infolist() if is_native(i.filename)]
        print(f"{rel}: {', '.join(libs)}")


if __name__ == "__main__":
    main()
