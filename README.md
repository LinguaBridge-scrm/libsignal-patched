# libsignal（补丁版）

Patched libsignal v0.103.0 JNI libraries for signal-cli 0.14.9: a configured proxy is never bypassed, and SOCKS5 hostnames are resolved by the proxy.

给 signal-cli 0.14.9 用的 libsignal v0.103.0 JNI 原生库，只打了两处网络补丁（[patches/libsignal-v0.103.0.patch](patches/libsignal-v0.103.0.patch)）：

1. `rust/net/src/connect_state.rs`：`infer_proxy_mode_for_config` 一律返回 `ProxyOnly`。原版把 Java 层传下来的 SOCKS / HTTP 代理都当成“系统代理”处理（`ProxyThenDirect`），代理断开或变慢时会改走直连。
2. `rust/net/infra/src/route/proxy.rs`：`socks5` 也一律交给代理解析域名（`resolve_hostname_locally: false`），本机不发 DNS 查询。

其余代码和编译参数与官方一致：在 GitHub Actions 上用 libsignal 自带的 `java/build_jni.sh desktop` 编译，工具链版本取 libsignal 的 `rust-toolchain`。

## 构件

[工作流](.github/workflows/build.yml)每次运行产出：

| 构件 | 内容 |
|---|---|
| `native-windows-x64` | `signal_jni_amd64.dll` |
| `native-macos-arm64` | `libsignal_jni_aarch64.dylib` |
| `native-linux-x64` | `libsignal_jni_amd64.so`（ubuntu 22.04 编译，glibc 2.35） |
| `libsignal-client-0.103.0-patched` | `libsignal-client-0.103.0.jar`（三个平台）、`<平台>/libsignal-client-0.103.0.jar`（只含该平台）、`SHA256SUMS`、`BUILD-INFO.txt`、补丁 |

用法：任选一个 jar，覆盖 `signal-cli-0.14.9/lib/libsignal-client-0.103.0.jar`。

- 原版 jar 里的 macOS x86_64 原生库没有补丁版，已删掉：在 Intel Mac 上会加载失败，而不是悄悄用回原版。
- 工作流最后在三个平台上各用替换后的 jar 跑一次冒烟测试（[scripts/Smoke.java](scripts/Smoke.java)：从 jar 里加载原生库并生成一对身份密钥）。

## Signal 运行时包（NexaChat 客户端用）

[runtime 工作流](.github/workflows/runtime.yml)把 signal-cli、jlink 裁剪的 Eclipse Temurin JRE 和上面的补丁版原生库组装成
`signal-runtime-<版本>-<target>.tar.gz`（target 为 `win32-x64`、`darwin-arm64`），手动运行时勾选 release 会连同 `SHA256SUMS`
发到 Release `signal-runtime-<版本>`。原生库取自 build 工作流某次成功运行的构件（默认 main 上最近一次），并核对补丁一致。

包内布局（[scripts/assemble_runtime.py](scripts/assemble_runtime.py)，NexaChat 按它找文件）：

| 路径 | 内容 |
|---|---|
| `VERSION.json` | `{signalCli, libsignal, patchCommit, jre, target, jvmArgs}`；`jvmArgs` 是要追加在 `-cp` 之前的 JVM 参数，`${native}`、`${root}` 换成 `native/` 与包根目录的绝对路径 |
| `jre/` | jlink 裁剪的 Temurin 25（模块和参数见脚本里的 `MODULES`、`JLINK_FLAGS`） |
| `lib/` | signal-cli 发行包的全部 jar；libsignal-client 换成补丁版。jar 里不含任何原生库（libsignal-client、sqlite-jdbc 的都已删掉） |
| `native/` | 本平台的原生库，随应用签名：补丁版 libsignal（`signal_jni.dll` / `libsignal_jni.dylib`）和 sqlite-jdbc 自带的那份（`sqlitejdbc.dll` / `libsqlitejdbc.dylib`） |
| `classpath.txt` | signal-cli 自带 `bin/signal-cli` 里 CLASSPATH 的顺序，一行一个 jar 文件名 |
| `licenses/` | signal-cli（GPLv3）、libsignal（AGPLv3，含 Rust 依赖致谢）、sqlite-jdbc（Apache-2.0）、Temurin（GPLv2 + Classpath Exception）的许可证和 `SOURCES.md` |

启动方式：`jre/bin/java --enable-native-access=ALL-UNNAMED -Djava.library.path=native <jvmArgs> -cp <classpath.txt 里的 jar> org.asamk.signal.Main …`。
jar 里没有原生库：libsignal 从 `java.library.path` 加载 `native/` 里的文件，sqlite-jdbc 按 `jvmArgs` 里的 `org.sqlite.lib.path`、
`org.sqlite.lib.name` 加载 `native/` 里的那份，都不解压到临时目录，Mac 上随应用一起签名。工作流在 Windows、macOS 机器上各实测一次
（[scripts/test_runtime.py](scripts/test_runtime.py)）：`--version`；不给参数时两个原生库都加载失败，按上面的方式启动就都从 `native/` 加载；
多账号 `jsonRpc` 下 `startLink` 拿到 `sgnl://linkdevice` 链接。

换 signal-cli、libsignal、JRE 或原生库时，先改工作流里的 `RUNTIME_VERSION`（`<signal-cli 版本>-<序号>`），已发的 Release 不覆盖。

## 本机编译（macOS Apple Silicon）

```bash
git clone --depth 1 --branch v0.103.0 https://github.com/signalapp/libsignal.git
cd libsignal
git apply ../patches/libsignal-v0.103.0.patch
brew install protobuf cmake
java/build_jni.sh desktop   # 产物：java/client/src/main/resources/libsignal_jni_aarch64.dylib
```

## 许可证

libsignal 以 AGPL-3.0-only 发布；本仓库的补丁和脚本同样以 AGPL-3.0-only 发布，全文见 [LICENSE](LICENSE)。
