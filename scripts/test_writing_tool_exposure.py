"""写作页常驻工具暴露的基线（P0 收束后）。

只做静态注册表检查：不建库、不联网、不调模型。钉住三件事：

1. 写作页可见 20 个工具（18 writing_* + Agent + self_explain），0 个 DEFERRED；
2. schema 总体积与 top 5 单工具体积有预算，新增工具不能静默膨胀；
3. ``agent.md`` 的工具段与注册表可见工具一致，且按阶段分组。

跑法：``python scripts/test_writing_tool_exposure.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools import PermissionMode, ToolExposure, ToolRegistry, ToolSpec  # noqa: E402
from writegraph.chat_tools import build_writing_registry               # noqa: E402

FAILED = 0

WRITING_TOOLS = {
    "writing_status", "writing_create_work", "writing_create_chapter",
    "writing_set_contract", "writing_read_chapter", "writing_set_mode",
    "writing_draft_chapter", "writing_replace_text", "writing_settle_chapter",
    "writing_references", "writing_research", "writing_docs",
    "writing_materials", "writing_memory", "writing_review_chapter",
    "writing_git", "writing_git_change", "writing_report",
}
MODEL_ONLY = {"Agent", "self_explain"}
EXPECTED = WRITING_TOOLS | MODEL_ONLY

# schema 字符预算（json.dumps(..., ensure_ascii=False)）；留约 5% 余量。
SCHEMA_BUDGET = 17_800
PER_TOOL_BUDGET = {
    "writing_materials": 2_800,
    "writing_git_change": 2_050,
    "writing_draft_chapter": 1_650,
    "writing_git": 1_400,
    "writing_research": 1_350,
}


def check(label: str, got, want) -> None:
    global FAILED
    if got == want:
        print(f"ok   {label}")
    else:
        FAILED += 1
        print(f"FAIL {label}\n     期望 {want!r}\n     实际 {got!r}")


def _fake_spec(name: str, toolset: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="base tool that must not enter the writing registry",
        parameters={"type": "object", "properties": {}},
        execute=lambda **_kw: "",
        toolset=toolset,
        exposure=ToolExposure.DIRECT,
        permission_mode=PermissionMode.READ_ONLY,
    )


def _schema_chars(spec: ToolSpec) -> int:
    return len(json.dumps(
        {"name": spec.name, "description": spec.description,
         "parameters": spec.parameters},
        ensure_ascii=False))


def _guide_tools(text: str) -> list[str]:
    section = text.rsplit("# 工具", 1)[-1]
    return re.findall(r"^- `([^`]+)`", section, re.M)


def test_registry() -> None:
    base = ToolRegistry()
    # 真实 base 里有 utility/file_ops 工具；写作页必须把它们全挡掉。
    base.register(_fake_spec("tool_search", "utility"))
    base.register(_fake_spec("read_file", "file_ops"))
    svc = SimpleNamespace(api_client=None, config=None)
    reg = build_writing_registry(base, svc, {})

    visible = reg.get_visible_specs()
    names = [s.name for s in visible]
    check("可见工具数量", len(visible), 20)
    check("可见工具名单", set(names), EXPECTED)
    check("没有重复注册", len(names), len(set(names)))
    check("没有 DEFERRED", reg.get_deferred_specs(), [])
    check("utility 工具被过滤", reg.get("tool_search"), None)
    check("file_ops 工具被过滤", reg.get("read_file"), None)

    for spec in visible:
        want = (ToolExposure.DIRECT_MODEL_ONLY if spec.name in MODEL_ONLY
                else ToolExposure.DIRECT)
        check(f"暴露级别：{spec.name}", spec.exposure, want)

    schemas = reg.get_schemas()
    check("schema 数量", len(schemas), 20)
    total = len(json.dumps(schemas, ensure_ascii=False))
    print(f"     schema 总量 {total} chars（预算 {SCHEMA_BUDGET}）")
    check("schema 总量在预算内", total <= SCHEMA_BUDGET, True)
    for name, budget in PER_TOOL_BUDGET.items():
        got = _schema_chars(reg.get(name))
        print(f"     {name}: {got} chars（预算 {budget}）")
        check(f"schema 预算：{name}", got <= budget, True)

    text = (ROOT / "prompts" / "writing" / "agent.md").read_text(
        encoding="utf-8")
    listed = _guide_tools(text)
    check("提示词工具段数量", len(listed), len(EXPECTED))
    check("提示词工具段与注册表一致", set(listed), EXPECTED)
    check("提示词工具段无重复", len(listed), len(set(listed)))

    section = text.rsplit("# 工具", 1)[-1]
    groups = re.findall(r"^\*\*([^*]+)\*\*$", section, re.M)
    print(f"     工具分组：{groups}")
    check("工具段按阶段分组", len(groups) >= 5, True)
    check("提示词没有旧术语", "生肉" not in text, True)
    check("有待归类自主找原块段",
          "### 待归类卡只有摘句时：自己找原块" in text, True)
    check("自主找块允许反复查", "不计调用次数" in text, True)


def main() -> int:
    test_registry()
    print()
    if FAILED:
        print(f"**{FAILED} 项失败**")
        return 1
    print("全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
