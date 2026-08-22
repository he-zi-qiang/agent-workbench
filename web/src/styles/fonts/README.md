# 随包发布的字体

从 Google Fonts 取的 `latin` 子集，不走 CDN 而是随包发布。为什么不走 CDN、
为什么只带拉丁，写在 `../tokens.css` 顶部的 `@font-face` 注释里。

**实际打进构建的只有 IBM Plex Mono 两个字重。** 判断依据是 `tokens.css` 里的
`@font-face`：Vite 只会 emit 被它引用到的文件，`dist/assets/` 里因此只有下表
标着"是"的那两个。

| 文件 | 来源 | 字重 | 大小 | 随包 |
|---|---|---|---|---|
| `ibm-plex-mono-latin-400.woff2` | IBM Plex Mono v20 · latin | 400 | 10 KB | 是 |
| `ibm-plex-mono-latin-500.woff2` | IBM Plex Mono v20 · latin | 500 | 10 KB | 是 |
| `space-grotesk-latin-var.woff2` | Space Grotesk v22 · latin | 可变 300–700 | 22 KB | 否 |

Space Grotesk 曾是标题字（`--aw-display`）。撤掉的理由是中英混排：拉丁标题会
突然切成另一套几何字形，而这是个桌面工具、不是落地页——层级现在交给字重、字距
和留白。文件留在树里而不是删掉，是因为"要不要有一支标题字"是个会被重新问起的
问题，而重新取分片、对 `unicode-range`、核版本号的成本远高于 22 KB。

它仍然被这个仓库分发，所以下面的署名对三个文件都成立，不因为它没进构建而作废。

三者均为 **SIL Open Font License 1.1**：

- Space Grotesk —— © Florian Karsten，<https://github.com/floriankarsten/space-grotesk>
- IBM Plex Mono —— © IBM Corp.，<https://github.com/IBM/plex>

OFL 允许随作品一起分发，条件是保留版权与许可声明（本文件即是），
且不以字体原名单独售卖。这里没有改动字形，文件是 Google Fonts 分片的原样拷贝。

要换版本，重新取分片并同步更新上表的版本号与大小——`unicode-range` 也要跟着
分片走，抄错会让某些字符静默落回系统字体。
