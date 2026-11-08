# 第三方来源登记

从其他项目拷贝或移植的代码，逐项登记来源和许可（[ADR 0006](docs/adr/0006-platform-and-reader-reuse.md) 第 5 条）。

## Agentero

- 上游：Agentero，MIT License，Copyright (c) 2026 poco-ai
- 参考副本：仓库外的 `../Agentero`，提交 `11219aa5e926b96377861bacf3774c8785612644`

| 本项目文件 | 上游文件 | 方式 |
|---|---|---|
| `src/litgleaner/domain/masking.py` | `src/lib/translate/mask.ts`、`src/lib/translate/preserved-links.ts` | 把占位符保护的匹配规则和还原校验从 TypeScript 移植为 Python |

### 许可声明

```
MIT License

Copyright (c) 2026 poco-ai

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
