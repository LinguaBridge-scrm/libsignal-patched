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
