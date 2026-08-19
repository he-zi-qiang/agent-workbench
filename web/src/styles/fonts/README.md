# 随包发布的字体

交接稿（`design_handoff_workbench_ui`）指定的两套字，从 Google Fonts 取的
`latin` 子集，不走 CDN 而是随包发布。为什么不走 CDN、为什么只带拉丁，
写在 `../tokens.css` 顶部的 `@font-face` 注释里。

| 文件 | 来源 | 字重 | 大小 |
|---|---|---|---|
| `space-grotesk-latin-var.woff2` | Space Grotesk v22 · latin | 可变 300–700 | 22 KB |
| `ibm-plex-mono-latin-400.woff2` | IBM Plex Mono v20 · latin | 400 | 10 KB |
| `ibm-plex-mono-latin-500.woff2` | IBM Plex Mono v20 · latin | 500 | 10 KB |

两者均为 **SIL Open Font License 1.1**：

- Space Grotesk —— © Florian Karsten，<https://github.com/floriankarsten/space-grotesk>
- IBM Plex Mono —— © IBM Corp.，<https://github.com/IBM/plex>

OFL 允许随作品一起分发，条件是保留版权与许可声明（本文件即是），
且不以字体原名单独售卖。这里没有改动字形，文件是 Google Fonts 分片的原样拷贝。

要换版本，重新取分片并同步更新上表的版本号与大小——`unicode-range` 也要跟着
分片走，抄错会让某些字符静默落回系统字体。
