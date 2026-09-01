import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound } from "lucide-react";
import { useState } from "react";
import {
  clearProviderKey,
  getProviderKey,
  storeProviderKey,
} from "../../api/client";
import { useIdentity } from "../../app/IdentityContext";
import { ErrorNotice, LoadingLine, errorMessage } from "../../components/ui";

/**
 * 模型密钥（ADR-101）。
 *
 * **两个布尔，不是一个。** 进程在组装时读一次 key，而 chat 那几条路由只有在那
 * 次读到了 key 时才会被挂上。所以「存进去了」和「正在用」是两个不同的问题，这
 * 一格分别回答它们——把它们并成一个「已配置」，正是一个设置页会声称刚存的 key
 * 已经生效、而用户回头发现 Chat 还是不在的那条路。
 *
 * **输入框里的东西永远只往一个方向走。** 服务端没有任何方法能返回明文，所以这
 * 里也不存在「把现有 key 填回输入框」这件事：能显示的只有四个字符的指纹，而那
 * 是服务端已经遮好递过来的。这一格因此没有一行遮蔽逻辑——没有可遮的东西经过它。
 *
 * **它在没有 key 的时候也必须能用。** 这是它不跟着 `serves_chat` 一起消失的原
 * 因：一个在没配 key 时就不出现的设置页，正好在唯一需要它的时刻不在。
 */
export function ProviderKeyPanel({ heading }: { heading?: React.ReactNode }) {
  const { identity } = useIdentity();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState("");

  const key = useQuery({
    queryKey: ["provider-key", identity.tenantId, identity.principalId],
    queryFn: () => getProviderKey(identity),
  });

  const invalidate = () =>
    void queryClient.invalidateQueries({ queryKey: ["provider-key"] });

  const store = useMutation({
    mutationFn: (value: string) => storeProviderKey(identity, value),
    onSuccess: () => {
      setDraft("");
      invalidate();
    },
  });
  const clear = useMutation({
    mutationFn: () => clearProviderKey(identity),
    onSuccess: invalidate,
  });

  const status = key.data;

  return (
    <>
      {heading ?? <h3>模型密钥</h3>}
      <p className="aw-settings-lede">
        这把 key 存在 checkout <b>之外</b>的一个文件里，打包工具不认 .gitignore，所以它不会跟着这个文件夹被压走。
      </p>
      <p className="aw-settings-lede">
        <b>存进去之后不会被读回来</b>：下面显示的四个字符是服务端遮好的指纹，接口本身没有返回明文的方法。
      </p>

      {status && !status.active ? (
        <p className="aw-notice">
          这次启动没有 key，Chat 与 Task 的模型调用不可用。
        </p>
      ) : null}

      {key.isPending ? <LoadingLine label="正在读密钥状态" /> : null}
      {key.isError ? <ErrorNotice message={errorMessage(key.error, "读不到密钥状态。")} /> : null}

      {status ? (
        <>
          {/* `.aw-setting-rows`，不是 `.aw-facts`——后者被这个文件引用过，而它在
              三份样式表里都不存在，于是这三行曾经是没有任何样式的裸 `<dl>`。 */}
          <dl className="aw-setting-rows">
            <div className="aw-setting-row">
              <div>
                <dt>这个进程正在用</dt>
                <small>模型客户端在启动时构造一次</small>
              </div>
              <dd>
                {status.active ? (
                  <span className="aw-code-value">
                    {status.fingerprint ?? "已配置"}
                  </span>
                ) : (
                  "没有"
                )}
              </dd>
            </div>
            <div className="aw-setting-row">
              <div>
                <dt>已存下，供下次启动</dt>
                <small>下次启动会读它</small>
              </div>
              <dd>
                {status.stored ? (
                  <span className="aw-code-value">
                    {status.fingerprint ?? "已存下"}
                  </span>
                ) : (
                  "没有存"
                )}
              </dd>
            </div>
            <div className="aw-setting-row">
              <div>
                <dt>存在哪</dt>
                <small>checkout 之外，权限 0600</small>
              </div>
              <dd>
                <span className="aw-code-value">
                  {status.path ?? "这台部署声明了「没有 key 文件」"}
                </span>
              </dd>
            </div>
          </dl>

          {status.restart_required ? (
            <p className="aw-notice is-warning">{status.restart_hint}</p>
          ) : null}
        </>
      ) : null}

      <form
        className="aw-form-stack"
        onSubmit={(event) => {
          event.preventDefault();
          if (draft.trim()) store.mutate(draft);
        }}
      >
        <label>
          <span>新的 API key</span>
          <input
            autoComplete="off"
            /* type="password" 而不是 text：这一格常常是在别人看得见屏幕的时候
               打开的，而一把贴进去还没提交的 key 和一把已经生效的 key 一样值钱。 */
            type="password"
            spellCheck={false}
            placeholder="sk-…"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
          />
        </label>
      </form>

      {store.isError ? <ErrorNotice message={errorMessage(store.error, "这把 key 没能存下。")} /> : null}
      {clear.isError ? <ErrorNotice message={errorMessage(clear.error, "没能删掉已存的 key。")} /> : null}

      <div className="aw-settings-actions">
        <button
          className="aw-button is-primary"
          disabled={!draft.trim() || store.isPending}
          onClick={() => store.mutate(draft)}
          type="button"
        >
          <KeyRound aria-hidden="true" size={15} />
          {store.isPending ? "正在保存…" : "保存"}
        </button>
        <button
          className="aw-button is-ghost"
          disabled={!status?.stored || clear.isPending}
          onClick={() => clear.mutate()}
          type="button"
        >
          {clear.isPending ? "正在删除…" : "删除已存的 key"}
        </button>
      </div>
    </>
  );
}
