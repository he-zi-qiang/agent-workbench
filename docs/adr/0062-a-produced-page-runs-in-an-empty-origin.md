# ADR-062：产出的页面在空 origin 里运行

- 决策点：Agent 产出的 HTML（Task 产物、Code 工作区文件）在控制台里怎么看——
  当纯文本源码（现状）、净化后静态展示、还是真实渲染运行；若运行，隔离边界
  建在哪一层
- 状态：**接受**。`previewKind` 新增 `html` 臂；新组件 `HtmlPreview` 用
  `<iframe srcdoc sandbox="allow-scripts">`（**刻意不含 `allow-same-origin`**）
  渲染，注入 meta CSP 作纵深；「渲染 / 源码」一键切换；SVG 维持 `<img>`
  栅格化不入此路径；两条下载路由补 `X-Content-Type-Options: nosniff`
- 日期：2026-08-16
- 影响：`web/src/components/media.ts`（词表 +`html`）、新
  `web/src/components/HtmlPreview.tsx`、`WorkPage`/`CodePage` 各接一臂、
  `adapters/tools/workspace.py` 与 `sandbox.py` 后缀表补 `.svg`、
  `apps/api/routes/artifacts.py` 与 `routes/code.py` 响应头
- 依赖：ADR-044（loopback 身份模型——身份是请求头，这条决定了方案选型）、
  ADR-045（预览端点只为 .docx 存在的论证，本 ADR 不推翻它）、
  用户决策 2026-08-16（「沙箱内可执行渲染」）

## 1. 背景：一个 HTML 产物今天两头落空

Agent 会产出 HTML——sandbox 跑出的图表页、Code 会话写的演示——而
`text/html` 在五值词表里落进 `text` 臂：Code 页显示 `<pre>` 源码，Work 页
更糟，`MarkdownContent` + `rehype-sanitize` 把标签消化殆尽，读者看到的既不是
页面也不是源码，是净化后的残渣。交互式页面唯一的验收方式是运行；一个只能
下载后另开浏览器才能看的 HTML 产物，对"点击即预览"的控制台是个洞。

## 2. 决定：sandbox 属性是边界，CSP 是纵深，服务端不开新路径

渲染面是 `<iframe srcdoc={html} sandbox="allow-scripts" referrerPolicy="no-referrer">`。
安全论证悬在一个**缺席**上：`sandbox` 值里没有 `allow-same-origin`，文档因此
拿到 opaque origin——没有父页 DOM，没有 cookie 和 storage，对平台 API 的
fetch 因带不上三个身份头（ADR-044）而全部 401。脚本随便跑，跑在一个什么都
没有的世界里。测试把属性值钉死，`BlobPreview` 有一条方向相反的对照测试钉死
它的 PDF 帧**没有** sandbox（`BlobPreview.test.tsx`）——两个钉子钉的是同一
件事：这个属性是被决定的，不是顺手写的。

**这条论证没有第二层，写清楚这一点比听起来稳妥更重要。** `about:srcdoc`
文档与 `blob:` URL 一样**继承父文档的 origin**；选 `srcdoc` 换来的是便利
（一个字符串，不用管 object URL 的生命周期），安全上什么都没多买。给这个
iframe 加上 `allow-same-origin`、或者整个去掉 `sandbox`，Agent 写的页面就
立刻拿到控制台自己的 origin：`parent.document`、存着的身份、以及读者凭据
下的每一条 `/v1/*`。这个文件里没有任何别的东西会拦住它。

（本 ADR 的初稿在这里写错过：它声称 srcdoc「从头就没有 origin 可继承」，
并据此说边界不悬在那个属性上。评审指出这与 HTML 规范不符——记在这里，
因为将来读这段的人正是可能为了让某个图表库跑起来而去动那个属性的人。）

注入的 meta CSP（`default-src 'none'; connect-src 'none'; form-action 'none';
base-uri 'none'`，内联脚本样式与 data:/blob: 资产放行）是纵深不是边界，
两个口子都要说明白：meta CSP 从解析点生效，恶意文档把脚本放在注入点之前
可以先行外联；而且**页面自导航**（`location.href = …`）不受任何一条 CSP
指令约束。如实记录这个残余：**平台数据不可达是硬保证（opaque origin +
无凭据），出网封锁是尽力而为**。Claude 桌面端用会话级 webRequest 拦截补上
这一层；浏览器 SPA 没有等价物，能补上它的方案被拒绝了（见 §3）。
控制台里给读者看的那句话按这个口径写，不多说一个字。

「渲染 / 源码」切换住在组件里，因为两个视图共享同一次 fetch；截断的正文
**拒绝渲染**（半个页面跑半份脚本，画出的东西从未存在过），只给源码并注明
截断。512KiB 上限沿用 `MAX_PREVIEW_BYTES`，超限先拒后取，一次传输都不花。

SVG 是明确的非目标：`<img>` 栅格化已经可见，脚本在 image 元素里不执行，
为极少数带脚本的 SVG 开第二个可执行面不值得。真正的 SVG 修复在后端：两张
后缀猜型表都缺 `.svg`（workspace 猜成 text/plain，sandbox 猜成
octet-stream），画出来的图连 image 臂都进不去——补表，让它先能被看见。

## 3. 被拒绝的方案

**服务端预览端点（真 CSP 响应头 + `CSP: sandbox` 指令）**。头比 meta 注入
健壮（从字节零生效，出网也封得死），但 iframe 的 src 请求带不上身份头，
需要一次性 token 或同源 cookie 的新鉴权通道；且 `routes/code.py` 的
docstring 论证过「一条路径一种行为才好授权」、`routes/artifacts.py` 从另一侧
论证过为何不做通用 preview 端点——为一层纵深推翻两处在案的安全论证，
代价与收益倒挂。客户端方案零后端改动，边界（opaque origin）一样硬。

**净化后静态展示**。零执行风险，但交互式页面的存在意义就是交互——图表
不动、按钮不响，读者验收不了任何东西，等于把这个功能做成另一种源码视图。

## 4. nosniff

两条下载路由（artifact 与 code 工作区）补 `X-Content-Type-Options: nosniff`。
与预览同一主张的服务端半边：存储时的 media type 就是全部答案，浏览器不得
把 text/plain 嗅探升格成可执行的东西。disposition 本就恒为 attachment，
nosniff 把最后一条翻案通道也关上。
