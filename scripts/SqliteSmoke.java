// SPDX-License-Identifier: AGPL-3.0-only
// 冒烟测试：以运行时包的 lib/ 为类路径，sqlite-jdbc 加载本平台的原生库（其他平台的已从 jar 里删掉），在内存库上查一次版本。
// 用法：javac -d <目录> scripts/SqliteSmoke.java，再用运行时包里的 jre/bin/java -cp "<目录><分隔符>lib/*" SqliteSmoke
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.Statement;

public class SqliteSmoke {
  public static void main(String[] args) throws Exception {
    try (Connection c = DriverManager.getConnection("jdbc:sqlite::memory:");
        Statement s = c.createStatement();
        ResultSet r = s.executeQuery("select sqlite_version()")) {
      r.next();
      System.out.println("sqlite " + r.getString(1) + " OK on " + System.getProperty("os.name") + " " + System.getProperty("os.arch"));
    }
  }
}
