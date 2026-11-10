"""自我说明工具：把产品卡片与本机状态组合成只读回答（决策 154）。"""
from __future__ import annotations

import json

from permissions import PermissionMode
from tools.registry import ToolExposure, ToolRegistry, ToolSpec

SELF_TOOLSET = "self"

_DESCRIPTION = (
    "回答关于留白自身的问题：功能是什么、在哪、为什么不可用。用户问"
    "「合同是什么 / 版本在哪 / 作者档案怎么是空的 / 为什么找不到素材」，"
    "或工具错误带 self_topic 时调用。question 传用户原话；错误联动时用 "
    "topic。只引用返回内容，matched=false 就说不知道、不要编。返回带 guard "
    "时只解释并请求确认，不要执行删除、回滚、合并或覆盖。"
)


def _spec(execute) -> ToolSpec:
    return ToolSpec(
        name="self_explain",
        category="utility",
        toolset=SELF_TOOLSET,
        exposure=ToolExposure.DIRECT_MODEL_ONLY,
        description=_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "用户的原话，例如「合同是啥」「改坏了能退吗」",
                },
                "topic": {
                    "type": "string",
                    "description": "工具错误里的 self_topic；与 question 二选一",
                },
                "depth": {
                    "type": "string",
                    "enum": ["short", "full"],
                    "description": "short=一句话与位置；用户追问再 full",
                },
            },
        },
        execute=execute,
        permission_mode=PermissionMode.READ_ONLY,
    )


def install_self_tool(registry: ToolRegistry, *, config=None,
                      bookbank_db=None) -> None:
    """在当前 registry 上安装/替换 self_explain。

    写作模式拿到的是 registry **副本**；必须在副本上重新安装，闭包才会
    读到副本的 ``reading_ctx``（writing_ctx / writing_svc / page）。
    只复制 ToolSpec 对象是不够的——闭包仍指向 base。
    """

    def _execute(question: str = "", topic: str = "",
                 depth: str = "short") -> str:
        try:
            from services.self_knowledge import explain
            ctx = dict(registry.reading_ctx or {})
            # CLI 不走 chat 路由，reading_ctx 里没有 config/db；用注册时
            # 捕获的实例兜底，保证「为什么没配置」在 CLI 也能答。
            if ctx.get("config") is None and config is not None:
                ctx["config"] = config
            if ctx.get("db") is None and bookbank_db is not None:
                ctx["db"] = bookbank_db
            payload = explain(question, depth=depth, topic=topic, ctx=ctx)
        except Exception as exc:                     # noqa: BLE001
            # 只报类型名：异常正文可能带路径或配置片段。
            payload = {"matched": False, "error": type(exc).__name__}
        return json.dumps(payload, ensure_ascii=False)

    registry.register(_spec(_execute))


def _register_self_tools(registry: ToolRegistry, *, config=None,
                         bookbank_db=None) -> None:
    install_self_tool(registry, config=config, bookbank_db=bookbank_db)
