/**
 * 一轮写完之后，哪个文件值得自己跳到读者眼前。
 *
 * **只有页面，不是所有产出。** 这条规则窄得像个偏好，但它是从「这一栏能对这份
 * 字节做什么」推出来的：`.py`、`.md`、`.csv` 在预览里是被*画出来*的，而读者在
 * 对话里已经看到了它们的卡片、名字和大小——自动打开只是把同一件事再说一遍，代价
 * 是抢走他正在读的那一栏。一个 `.html` 不一样：它在那一栏里是被*跑起来*的
 * （`HtmlPreview` 的沙箱帧），而一个没被跑起来的页面等于没做出来——读者拿到的是
 * 一份他看不懂也用不了的源码，除非他知道要去点哪里。
 *
 * 类型按名字猜，和 `ProjectFileBody` 用同一张表、同一个理由：项目文件在服务端
 * 根本没有 media type，而工作区文件有——所以调用方给得出来就给，给不出来就退回
 * 名字。两条路都走 `effectiveMediaType`，于是 `.html` 和 `.htm` 在这里和在别处
 * 是同一件事。
 *
 * 纯函数，没有 React：这样「该不该弹」可以被单独测，而页面上那段只剩「弹」。
 */

import { effectiveMediaType, previewKind } from "../../components/media";

/**
 * 这些名字里最后一个能跑的页面，没有就是 `null`。
 *
 * 最后一个而不是第一个：一轮里写出 `style.css`、`app.js`、`index.html` 是常见的
 * 顺序，而读者要看的是收尾的那个。同一轮里重写同一个名字也落在这条规则上——
 * 名字相同，候选就没变，页面那侧因此不会重复弹。
 */
export function pageAmong(
  names: readonly string[],
  mediaTypeOf?: (name: string) => string | undefined,
): string | null {
  for (let index = names.length - 1; index >= 0; index -= 1) {
    const name = names[index];
    if (name === undefined) continue;
    const declared = mediaTypeOf?.(name) ?? "";
    if (previewKind(effectiveMediaType(declared, name)) === "html") return name;
  }
  return null;
}

/**
 * 项目里一个相对路径的上一级，`""` 表示项目根。
 *
 * 在这里做，而不是在页面里：路径算术只该有一个地方，而这是目录列表那条请求
 * 唯一需要的那一点——要拿到刚写出的那个文件的字节数（预览在读正文之前用它挡
 * 太大的文件），只能问它所在的那一层。
 */
export function parentOf(path: string): string {
  const cut = path.lastIndexOf("/");
  return cut === -1 ? "" : path.slice(0, cut);
}
