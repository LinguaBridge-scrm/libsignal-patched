// SPDX-License-Identifier: AGPL-3.0-only
// 冒烟测试：以 signal-cli 的 lib/ 为类路径，让 libsignal 从 jar 里取出本平台的原生库并加载，再调用一次（生成一对身份密钥）。
// jar 里缺本平台的原生库、或原生库加载不了时，这里会抛异常、以非零退出。
// 用法：java --enable-native-access=ALL-UNNAMED -cp "signal-cli-0.14.9/lib/*" scripts/Smoke.java
import org.signal.libsignal.protocol.IdentityKeyPair;

public class Smoke {
  public static void main(String[] args) {
    byte[] publicKey = IdentityKeyPair.generate().getPublicKey().serialize();
    if (publicKey.length != 33) {
      throw new IllegalStateException("unexpected public key length " + publicKey.length);
    }
    System.out.println("libsignal native library OK on " + System.getProperty("os.name") + " " + System.getProperty("os.arch"));
  }
}
