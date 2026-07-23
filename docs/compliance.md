# 合规基线

## 源码边界

产品代码必须根据本工作区中的公开架构和契约文档独立编写。来源不确定或泄漏
的 Claude Code 材料只能用于高层行为分析，不能复制、逐行翻译、vendoring，
也不能作为生成产品实现的代码提示内容。

使用官方公开仓库时必须遵守其已发布许可证；任何改编代码都必须保留要求的
声明和署名。

## 依赖策略

- `pyproject.toml` 是直接依赖的唯一声明；
- `uv.lock` 是解析后依赖图的唯一事实源；
- Runtime 启动时继续执行独立的安全版本下限检查；
- Optional Labs 不进入 production 主依赖路径；
- CI 使用锁定版本的 `pip-licenses` 执行依赖许可证 allowlist；
- CI 下载 MIT 许可的 Gitleaks CLI 时校验固定 SHA-256，再扫描完整 Git 历史；
- 引入第一个外部框架 Adapter 前，机器可读的许可证与漏洞报告必须成为
  merge gate。

## 密钥策略

真实凭据、DSN、`.env`、mounted secret 内容、prompt 正文和 Tool 结果不能
进入 Git 历史、日志、事件、trace、测试报告或证据产物。

## 能力声明

README 与简历中的能力状态必须遵循：

```text
Planned → Implemented → Tested → Demonstrated
```

没有可链接证据时，能力状态不能升级。
