# ADR 0002：更名为拾穗 LitGleaner，以 MIT 许可证开源

- 状态：已接受

## 背景

项目要分发给其他研究者独立运行，需要一个公开仓库和便于记忆的名字。原名“论文工厂”（包名 `litfactory`）强调批量生产，而系统实际做的是从大量新文献里挑出少数值得读的几篇，再帮研究者读懂、留下笔记。

## 决定

1. **中文名“拾穗”，英文名 LitGleaner。** 取“拾穗者”的意象：每天在文献的麦田里拾起值得读的那几穗。
2. **代码一起改名。**
   - Python 包 `litfactory` → `litgleaner`，命令行 `litfactory` → `litgleaner`。
   - 环境变量前缀 `LITFACTORY_` → `LITGLEANER_`；根异常 `LitFactoryError` → `LitGleanerError`。
   - 数据目录名、本地密钥文件目录（`~/.config/litgleaner/`）、钥匙串条目名、HTTP User-Agent 和 Zotero 默认分组随之改名。
3. **本机运维接口保持不变。** `scripts/autoloop_run.py` 发出的事件类型 `litfactory.failed` 和 agent-autoloop 里的来源名是本机 agent-autoloop 配置里的标识，不属于产品功能，不随本次改名变化，避免本机规则失配。
4. **以 MIT 许可证开源**，仓库名 `litgleaner`。
5. **图标**：深绿底上的一支金色麦穗，旁边一粒拾起的亮色麦粒。源文件放在 `docs/assets/`，桌面安装包的图标从这里导出。

## 后果

- 旧名的本地数据不会自动迁移：开发数据库文件改名为 `litgleaner.db`；已授权的 Zotero 本地 key 需要从 `~/.config/litfactory/` 移到 `~/.config/litgleaner/`，或重新运行 `litgleaner zotero authorize`。
- 已设置的 `LITFACTORY_CONTACT_EMAIL`、`LITFACTORY_CONFIG` 要改成新前缀。
- 本地仓库目录名不受影响。
