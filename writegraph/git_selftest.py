"""写作 git 自测清单 —— 由 ``writing_git(action='selftest_plan')`` 下发。

清单是**数据**，不是代码：每一步给「怎么做」和「怎么算通过」。agent 按
阶段执行，不符合 ``pass`` 的就用 ``writing_report`` 记一条，最后汇报。

为什么清单不写进 ``agent.md``：它会随实现变长，占每轮上下文；放在这里
既能让 agent 随时取最新版，也方便改一条测试就改一处。

占位符写法 ``$seed.work_id`` / ``$A3.commit_id`` 表示「前面某一步返回的
值」。agent 执行时用自己看到的值替换，不要原样传给工具。
"""

from __future__ import annotations

from typing import Any

PROMPT = (
    "写作 git 的自测清单。只在 selftest_seed 新建的临时作品里操作；"
    "每一步按 do 调用工具，再用 pass 里的标准核对返回值。"
    "清单很长，按阶段取（phase=A/B/C/D），不要一次取全量。"
    "不符合就是功能问题，记进 writing_report，不要自行绕过或改库。"
)

STEPS: list[dict[str, Any]] = [
    # ── A 基础：存档 / 单章历史 / 修正 ──────────────────────────────────
    {
        "id": "A1", "phase": "A 基础",
        "title": "建立自测工作区",
        "do": [{"tool": "writing_git", "args": {"action": "selftest_seed"}}],
        "pass": [
            "返回 ok=true，有 work_id、3 章、baseline_commit",
            "返回的 anchors 里有甲~庚七个锚点",
        ],
        "note": "之后所有操作都在这个 work_id 上；绝不碰作者正在写的书。",
    },
    {
        "id": "A2", "phase": "A 基础",
        "title": "基线状态",
        "do": [
            {"tool": "writing_git", "args": {"action": "status",
                                             "work_id": "$seed.work_id"}},
            {"tool": "writing_git", "args": {"action": "log",
                                             "work_id": "$seed.work_id"}},
        ],
        "pass": [
            "clean=true；branch=main",
            "history 只有 1 条，message=自测基线，is_head=true",
            "branches 里 main 的 is_head=true",
        ],
    },
    {
        "id": "A3", "phase": "A 基础",
        "title": "工作区改动与存档",
        "do": [
            {"tool": "writing_replace_text",
             "args": {"chapter_index": 1, "anchor_quote": "风从码头来",
                      "replacement": "风从码头来（改）"}},
            {"tool": "writing_git", "args": {"action": "status",
                                             "work_id": "$seed.work_id"}},
            {"tool": "writing_git_change",
             "args": {"action": "commit", "work_id": "$seed.work_id",
                      "message": "A3：改第一章开头"}},
        ],
        "pass": [
            "替换后 status.clean=false，changes.changed 里正好 1 章",
            "存档返回 status.clean=true，commit.message 以 A3 开头",
        ],
    },
    {
        "id": "A4", "phase": "A 基础",
        "title": "单章历史（log -- <章>）",
        "do": [
            {"tool": "writing_git", "args": {"action": "log",
                                             "work_id": "$seed.work_id",
                                             "chapter_index": 1}},
            {"tool": "writing_git", "args": {"action": "log",
                                             "work_id": "$seed.work_id",
                                             "chapter_index": 3}},
        ],
        "pass": [
            "第 1 章的历史有 2 条（基线 + 改第一章开头）",
            "第 3 章的历史只有 1 条（基线）",
        ],
    },
    {
        "id": "A5", "phase": "A 基础",
        "title": "修正上一次存档（amend）",
        "do": [
            {"tool": "writing_replace_text",
             "args": {"chapter_index": 1, "anchor_quote": "他数着潮声",
                      "replacement": "他数着潮声（补）"}},
            {"tool": "writing_git_change",
             "args": {"action": "amend", "work_id": "$seed.work_id",
                      "message": "A5：改第一章开头（补）"}},
            {"tool": "writing_git", "args": {"action": "log",
                                             "work_id": "$seed.work_id"}},
            {"tool": "writing_git", "args": {"action": "reflog",
                                             "work_id": "$seed.work_id"}},
        ],
        "pass": [
            "history 仍然是 2 条，没有被 amend 出第三条",
            "HEAD 的 message 以 A5 开头，且包含刚补的正文",
            "clean=true",
            "reflog 里有一条 reason=amend，from 是旧存档 id",
        ],
    },
    # ── B 分支 / 差异 / 合并 ────────────────────────────────────────────
    {
        "id": "B1", "phase": "B 分支与合并",
        "title": "开岔路并切过去",
        "do": [
            {"tool": "writing_git_change",
             "args": {"action": "branch_create", "work_id": "$seed.work_id",
                      "name": "岔路A", "switch": True}},
            {"tool": "writing_replace_text",
             "args": {"chapter_index": 1,
                      "anchor_quote": "风从码头来（改）",
                      "replacement": "岔路甲"}},
            {"tool": "writing_git_change",
             "args": {"action": "commit", "work_id": "$seed.work_id",
                      "message": "B1：岔路改开头"}},
            {"tool": "writing_git", "args": {"action": "status",
                                             "work_id": "$seed.work_id"}},
        ],
        "pass": [
            "branch_create 返回 switched=true、status.branch=岔路A",
            "存档返回 clean=true；最后这次 status 里 branches 的岔路A "
            "is_head=true",
        ],
    },
    {
        "id": "B2", "phase": "B 分支与合并",
        "title": "切回主线，正文应回退",
        "do": [
            {"tool": "writing_git_change",
             "args": {"action": "branch_switch", "work_id": "$seed.work_id",
                      "name": "main"}},
            {"tool": "writing_read_chapter", "args": {"chapter_index": 1}},
        ],
        "pass": [
            "branch_switch 返回 branch=main，clean=true",
            "第 1 章正文是「风从码头来（改）」，不是「岔路甲」",
            "branch_switch 返回的 branches 里岔路A 显示 ahead=1、"
            "base_short 非空",
        ],
    },
    {
        "id": "B3", "phase": "B 分支与合并",
        "title": "三点差异（A...B）",
        "do": [
            {"tool": "writing_git", "args": {"action": "diff",
                                             "work_id": "$seed.work_id",
                                             "from_ref": "main...岔路A"}},
        ],
        "pass": [
            "changes.changed 正好 1 章（第 1 章）",
            "previews 里第 1 章 head 含「岔路甲」，且不含「风从码头来」"
            "（目标侧是岔路A；不用再切分支读章）",
        ],
    },
    {
        "id": "B4", "phase": "B 分支与合并",
        "title": "摘取岔路的一次存档（cherry-pick）",
        "do": [
            {"tool": "writing_git", "args": {"action": "log",
                                             "work_id": "$seed.work_id",
                                             "all_branches": True}},
            {"tool": "writing_git_change",
             "args": {"action": "cherry_pick", "work_id": "$seed.work_id",
                      "commit_id": "$B4.岔路A最新存档id"}},
        ],
        "pass": [
            "摘取返回的 chapters 里第 1 章正文=「岔路甲」"
            "（看 head/tail，不用再读整章）",
            "返回 commit.kind=cherry-pick，status.clean=true",
            "岔路A 的存档仍在（前面的 log --all 里能看到）",
        ],
    },
    {
        "id": "B5", "phase": "B 分支与合并",
        "title": "快进合并",
        "do": [
            {"tool": "writing_git_change",
             "args": {"action": "branch_create", "work_id": "$seed.work_id",
                      "name": "快进路", "switch": True}},
            {"tool": "writing_replace_text",
             "args": {"chapter_index": 2, "anchor_quote": "配角没有抬头",
                      "replacement": "快进戊"}},
            {"tool": "writing_git_change",
             "args": {"action": "commit", "work_id": "$seed.work_id",
                      "message": "B5：快进路改第二章"}},
            {"tool": "writing_git_change",
             "args": {"action": "branch_switch", "work_id": "$seed.work_id",
                      "name": "main"}},
            {"tool": "writing_git_change",
             "args": {"action": "merge", "work_id": "$seed.work_id",
                      "branch": "快进路"}},
        ],
        "pass": [
            "合并返回 fast_forward=true，status.clean=true",
            "合并返回的 chapters 里第 2 章正文=「快进戊」"
            "（看 head/tail，不用再读整章）",
            "main 的 head 与 快进路 的 tip 是同一个存档"
            "（status.branch / status.head 能核对）",
        ],
    },
    {
        "id": "B6", "phase": "B 分支与合并",
        "title": "三路合并（两侧改不同章）",
        "do": [
            {"tool": "writing_git_change",
             "args": {"action": "branch_create", "work_id": "$seed.work_id",
                      "name": "三路", "switch": True}},
            {"tool": "writing_replace_text",
             "args": {"chapter_index": 3, "anchor_quote": "雨落在篷布上",
                      "replacement": "岔路己"}},
            {"tool": "writing_git_change",
             "args": {"action": "commit", "work_id": "$seed.work_id",
                      "message": "B6：三路改第三章"}},
            {"tool": "writing_git_change",
             "args": {"action": "branch_switch", "work_id": "$seed.work_id",
                      "name": "main"}},
            {"tool": "writing_replace_text",
             "args": {"chapter_index": 3, "anchor_quote": "他把信折好",
                      "replacement": "主线庚"}},
            {"tool": "writing_git_change",
             "args": {"action": "commit", "work_id": "$seed.work_id",
                      "message": "B6：主线改第三章结尾"}},
            {"tool": "writing_git_change",
             "args": {"action": "merge", "work_id": "$seed.work_id",
                      "branch": "三路"}},
        ],
        "pass": [
            "合并返回 fast_forward=false，且 commit.parent2_id 非空",
            "合并返回的 chapters 里第 3 章同时包含「岔路己」和「主线庚」",
            "status.clean=true；commit.branch=main",
        ],
    },
    # ── C 冲突 / 撤销 / 回到某一版 ─────────────────────────────────────
    {
        "id": "C1", "phase": "C 撤销与恢复",
        "title": "制造冲突，先不落库",
        "do": [
            {"tool": "writing_git_change",
             "args": {"action": "branch_create", "work_id": "$seed.work_id",
                      "name": "冲突路", "switch": True}},
            {"tool": "writing_replace_text",
             "args": {"chapter_index": 1, "anchor_quote": "岔路甲",
                      "replacement": "岔路甲2"}},
            {"tool": "writing_git_change",
             "args": {"action": "commit", "work_id": "$seed.work_id",
                      "message": "C1：冲突路改同一行"}},
            {"tool": "writing_git_change",
             "args": {"action": "branch_switch", "work_id": "$seed.work_id",
                      "name": "main"}},
            {"tool": "writing_replace_text",
             "args": {"chapter_index": 1, "anchor_quote": "岔路甲",
                      "replacement": "主线甲2"}},
            {"tool": "writing_git_change",
             "args": {"action": "commit", "work_id": "$seed.work_id",
                      "message": "C1：主线改同一行"}},
            {"tool": "writing_git_change",
             "args": {"action": "merge", "work_id": "$seed.work_id",
                      "branch": "冲突路"}},
        ],
        "pass": [
            "合并返回 applied=false、needs_resolution=true",
            "conflicts 里有一条 kind=chapter，key 形如 chapter:<id>",
            "工作区没有被改脏：返回的 status.clean=true，第 1 章仍是"
            "「主线甲2」（看 conflicts 里的 ours_preview，不用读整章）",
        ],
    },
    {
        "id": "C2", "phase": "C 撤销与恢复",
        "title": "裁决冲突并完成合并",
        "do": [
            {"tool": "writing_git_change",
             "args": {"action": "merge", "work_id": "$seed.work_id",
                      "branch": "冲突路",
                      "resolutions": {"$C1.conflict.key": "theirs"}}},
        ],
        "pass": [
            "这一次合并成功，返回 commit.parent2_id 非空",
            "返回的 chapters 里第 1 章正文等于 conflicts 里指名的那一侧"
            "（theirs 即冲突路侧），不是两边拼在一起的文本",
            "status.clean=true",
        ],
        "note": "resolutions 的键必须是 C1 返回的 conflict.key 原文，不要自己拼。",
    },
    {
        "id": "C3", "phase": "C 撤销与恢复",
        "title": "撤销某一次存档（revert）",
        "do": [
            {"tool": "writing_replace_text",
             "args": {"chapter_index": 2, "anchor_quote": "快进戊",
                      "replacement": "要撤销的戊"}},
            {"tool": "writing_git_change",
             "args": {"action": "commit", "work_id": "$seed.work_id",
                      "message": "C3：这一版要撤销"}},
            {"tool": "writing_git_change",
             "args": {"action": "revert", "work_id": "$seed.work_id",
                      "commit_id": "$C3.commit.id"}},
            {"tool": "writing_git_change",
             "args": {"action": "revert", "work_id": "$seed.work_id",
                      "commit_id": "$C3.commit.id"}},
        ],
        "pass": [
            "第一次 revert 返回的 chapters 里第 2 章回到「快进戊」",
            "返回的 commit.kind=revert、parent_id 非空（历史没被抹掉，"
            "是新存档反向撤销）",
            "再次撤销同一存档返回错误「已经撤销过了」，不是再产生一条空存档",
        ],
        "note": "$C3.commit.id 是「C3：这一版要撤销」那条存档的 id"
                "（第一次 revert 返回的是新存档，别拿错）；不要为了取号再查 log。",
    },
    {
        "id": "C4", "phase": "C 撤销与恢复",
        "title": "回到某一版（restore）",
        "do": [
            {"tool": "writing_replace_text",
             "args": {"chapter_index": 3, "anchor_quote": "岔路己",
                      "replacement": "岔路己（坏）"}},
            {"tool": "writing_git_change",
             "args": {"action": "commit", "work_id": "$seed.work_id",
                      "message": "C4：这一版写坏了"}},
            {"tool": "writing_git_change",
             "args": {"action": "restore", "work_id": "$seed.work_id",
                      "commit_id": "$C4.commit.parent_id"}},
            {"tool": "writing_git_change",
             "args": {"action": "restore", "work_id": "$seed.work_id",
                      "commit_id": "$C4.commit.parent_id"}},
        ],
        "pass": [
            "第一次 restore 返回的 chapters 里第 3 章不再含「（坏）」",
            "返回的 commit.kind=restore，status.clean=true",
            "再次回滚到同一版返回「已经在目标状态上」",
        ],
        "note": "C4.commit.parent_id 是「C4：这一版写坏了」那条存档的"
                " parent_id（= 改坏之前的 HEAD；第一次 restore 的返回值别拿）；"
                "不要再查 log，也不用读整章。",
    },
    {
        "id": "C5", "phase": "C 撤销与恢复",
        "title": "只丢弃一章的未存档改动",
        "do": [
            {"tool": "writing_replace_text",
             "args": {"chapter_index": 3, "anchor_quote": "主线庚",
                      "replacement": "主线庚（乱改）"}},
            {"tool": "writing_git", "args": {"action": "status",
                                             "work_id": "$seed.work_id"}},
            {"tool": "writing_git_change",
             "args": {"action": "discard_chapter",
                      "work_id": "$seed.work_id", "chapter_index": 3}},
        ],
        "pass": [
            "丢弃前的 status 里 changes.changed 只有第 3 章",
            "丢弃返回的 chapter 里第 3 章回到「主线庚」，status.clean=true",
            "账本没有被这次丢弃改动（status.ledger_dirty=false）",
        ],
    },
    # ── D 暂存 / 标签 / 分支维护 / reflog / 反馈 ───────────────────────
    {
        "id": "D1", "phase": "D 收尾",
        "title": "暂存与恢复",
        "do": [
            {"tool": "writing_replace_text",
             "args": {"chapter_index": 2, "anchor_quote": "船期写在木牌上",
                      "replacement": "船期写在木牌上（暂存）"}},
            {"tool": "writing_git_change",
             "args": {"action": "stash_push", "work_id": "$seed.work_id",
                      "message": "自测暂存"}},
            {"tool": "writing_git", "args": {"action": "stash_list",
                                             "work_id": "$seed.work_id"}},
            {"tool": "writing_replace_text",
             "args": {"chapter_index": 2, "anchor_quote": "快进戊",
                      "replacement": "快进戊（暂存后）"}},
            {"tool": "writing_git_change",
             "args": {"action": "commit", "work_id": "$seed.work_id",
                      "message": "D1：暂存之后又改了一处"}},
            {"tool": "writing_git_change",
             "args": {"action": "stash_pop", "work_id": "$seed.work_id",
                      "name": "$D1.stash name"}},
            {"tool": "writing_git", "args": {"action": "stash_list",
                                             "work_id": "$seed.work_id"}},
        ],
        "pass": [
            "stash_push 返回 status.clean=true、counts.changed=0"
            "（工作区回到 HEAD，暂存的改动不在正文里）",
            "stash_list 里有 1 条，名字形如 stash/cm-…",
            "stash_pop 返回的 chapters 里「船期写在木牌上（暂存）」回来，"
            "同时「快进戊（暂存后）」没被退回",
            "stash_list 变空；stash_pop 的 status.clean=false、"
            "counts.changed=1（暂存内容回到工作区，尚未存档——pop 不是丢弃）",
        ],
        "note": "这一条专门盯「整树覆盖」回归：恢复暂存不能把暂存之后存档的改动吃掉。",
    },
    {
        "id": "D2", "phase": "D 收尾",
        "title": "标签",
        "do": [
            {"tool": "writing_git_change",
             "args": {"action": "tag_create", "work_id": "$seed.work_id",
                      "name": "v1自测"}},
            {"tool": "writing_git", "args": {"action": "tags",
                                             "work_id": "$seed.work_id"}},
            {"tool": "writing_git", "args": {"action": "log",
                                             "work_id": "$seed.work_id",
                                             "limit": 1}},
            {"tool": "writing_git_change",
             "args": {"action": "tag_delete", "work_id": "$seed.work_id",
                      "name": "v1自测"}},
        ],
        "pass": [
            "打标签后 tags 里有 v1自测，指向当前 HEAD",
            "最新一条历史带标签装饰",
            "删除后 tags 为空；再删一次返回「标签不存在」",
        ],
    },
    {
        "id": "D3", "phase": "D 收尾",
        "title": "分支改名与删除（含未合并保护）",
        "do": [
            {"tool": "writing_git_change",
             "args": {"action": "branch_rename", "work_id": "$seed.work_id",
                      "name": "三路", "new_name": "三路改名"}},
            # 改名是否生效要在删除之前看：删掉之后就查不到这条分支了。
            {"tool": "writing_git", "args": {"action": "branches",
                                             "work_id": "$seed.work_id"}},
            {"tool": "writing_git_change",
             "args": {"action": "branch_delete", "work_id": "$seed.work_id",
                      "name": "三路改名"}},
            {"tool": "writing_git_change",
             "args": {"action": "branch_delete", "work_id": "$seed.work_id",
                      "name": "main"}},
            {"tool": "writing_git_change",
             "args": {"action": "branch_create", "work_id": "$seed.work_id",
                      "name": "废弃路", "switch": True}},
            {"tool": "writing_read_chapter", "args": {"chapter_index": 1}},
            {"tool": "writing_replace_text",
             "args": {"chapter_index": 1,
                      "anchor_quote": "<用上一步读到的第一章原文>",
                      "replacement": "废弃路版"}},
            {"tool": "writing_git_change",
             "args": {"action": "commit", "work_id": "$seed.work_id",
                      "message": "D3：废弃路一改"}},
            {"tool": "writing_git_change",
             "args": {"action": "branch_switch", "work_id": "$seed.work_id",
                      "name": "main"}},
            {"tool": "writing_git_change",
             "args": {"action": "branch_delete", "work_id": "$seed.work_id",
                      "name": "废弃路"}},
            {"tool": "writing_git_change",
             "args": {"action": "branch_delete", "work_id": "$seed.work_id",
                      "name": "废弃路", "force": True}},
            {"tool": "writing_git", "args": {"action": "branches",
                                             "work_id": "$seed.work_id"}},
            {"tool": "writing_read_chapter", "args": {"chapter_index": 1}},
        ],
        "pass": [
            "改名后、删除前的 branches 里是三路改名，不再是三路",
            "已合并的三路改名可以直接删除",
            "删除 main 被拒绝（当前分支）",
            "未合并的废弃路不带 force 被拒绝，带 force 成功",
            "最后的 branches 里没有废弃路、main 仍在；强制删除只删分支指针："
            "main 的第 1 章仍是合并后的原文，不被废弃路的「废弃路版」污染",
        ],
    },
    {
        "id": "D4", "phase": "D 收尾",
        "title": "reflog 与分支元信息",
        "do": [
            {"tool": "writing_git", "args": {"action": "reflog",
                                             "work_id": "$seed.work_id",
                                             "limit": 50}},
            {"tool": "writing_git", "args": {"action": "branches",
                                             "work_id": "$seed.work_id"}},
        ],
        "pass": [
            "reflog 里能找到 checkout / merge / revert / restore / amend 的记录",
            "每条记录都有 commit_short 与非空 reason",
            "branches 里岔路A 有 base_short、ahead/behind 数字",
        ],
    },
    {
        "id": "D5", "phase": "D 收尾",
        "title": "自动检查点分支删除保护",
        "do": [
            {"tool": "writing_git_change",
             "args": {"action": "branch_create", "work_id": "$seed.work_id",
                      "name": "检查点路", "switch": True}},
            {"tool": "writing_replace_text",
             "args": {"chapter_index": 1,
                      "anchor_quote": "他数着潮声",
                      "replacement": "他数着潮声（检查点路存档）"}},
            {"tool": "writing_git_change",
             "args": {"action": "commit", "work_id": "$seed.work_id",
                      "message": "D5：检查点路先存档"}},
            {"tool": "writing_replace_text",
             "args": {"chapter_index": 1,
                      "anchor_quote": "他数着潮声（检查点路存档）",
                      "replacement": "他数着潮声（未保存）"}},
            {"tool": "writing_git_change",
             "args": {"action": "branch_switch", "work_id": "$seed.work_id",
                      "name": "main"}},
            {"tool": "writing_git_change",
             "args": {"action": "branch_delete", "work_id": "$seed.work_id",
                      "name": "检查点路", "force": True}},
            {"tool": "writing_git", "args": {"action": "tags",
                                             "work_id": "$seed.work_id"}},
        ],
        "pass": [
            "branch_switch 成功切回 main；切走前的未存档改动被自动存档成"
            "检查点（留在检查点路）",
            "force 删除检查点路后，返回 recovered_tag=lost-检查点路",
            "tags 里有 lost-检查点路，指向那个检查点；不能静默丢掉未保存改动",
        ],
        "note": "这一条守「作者以为改动还在、其实没了」：检查点只挂在被删"
                "分支上时，删除前必须留一个可恢复的引用。",
    },
    {
        "id": "D6", "phase": "D 收尾",
        "title": "把发现记下来",
        "do": [
            {"tool": "writing_report",
             "args": {"action": "list", "status": "open"}},
        ],
        "pass": [
            "前面有失败/建议时：先用 action=add 逐条记下（kind/area/severity/"
            "title/body，body 写清哪一步、期望什么、实际什么、怎么复现），再 list 核对",
            "全部通过时：不要 add 假问题，list 为空是正常结果",
            "最后给作者三行总结：通过几条、失败几条、最值得先修的一条",
        ],
        "note": "反馈是给实现者的工单，不是给作者的读后感；只记真实偏差。",
    },
]


_PHASES = {"A": "A 基础", "B": "B 分支与合并",
           "C": "C 撤销与恢复", "D": "D 收尾"}


def plan(phase: str = "", step: str = "") -> dict[str, Any]:
    """给 agent 的清单。``phase`` / ``step`` 用来只取当前要做的那一段。

    全量清单有 22 步、约 18KB；一轮 ReAct 里每多带一轮就重发一次，
    而跨轮历史又不会保留工具结果。按阶段取（A/B/C/D）能把单次清单
    压到 3-8KB，也让「上一轮做到哪」不依赖模型记忆。
    """
    phase = (phase or "").strip()
    step = (step or "").strip().upper()
    steps = STEPS
    if step:
        steps = [s for s in STEPS if str(s.get("id", "")).upper() == step]
        if not steps:
            raise ValueError(
                f"未知步骤：{step}；可选 " + "、".join(s["id"] for s in STEPS))
    elif phase:
        key = phase.upper()
        # 接受 A / a / "A 基础" / "基础" 这几种写法。
        steps = [s for s in STEPS
                 if str(s.get("id", ""))[:1].upper() == key
                 or str(s.get("phase", "")) == phase
                 or str(s.get("phase", "")).startswith(phase)
                 or phase in str(s.get("phase", ""))]
        if not steps:
            raise ValueError(
                f"未知阶段：{phase}；可选 A / B / C / D（"
                + "、".join(_PHASES.values()) + "）")
    out = {
        "goal": "按「作者会怎么用」验证写作 git：存档、分支、合并、冲突、"
                "撤销、回滚、暂存、标签、reflog，以及每条指令的失败保护。",
        "rules": [
            "只在 selftest_seed 新建的临时作品里操作",
            "按 A → B → C → D 顺序执行；同一阶段内不要跳步",
            "每步先按 do 调用，再用 pass 核对；不符合就记 issue 并继续",
            "占位符 $C3.commit.id 表示 C3 那一步返回的 commit.id，用实际值"
            "替换；commit/merge/restore 的返回值已经带 id，不要再查 log 取号",
            "变更操作返回的 status/chapters 就是核对依据；不要紧接着再查"
            "status 或 read_chapter",
            "冲突裁决只在清单要求时做；真实写作中永远先问作者",
            "不要为了「让测试通过」绕开工具直接改库",
            "全部通过后问作者是否清理临时作品（selftest_cleanup）；"
            "有失败时保留，便于复现",
        ],
        "steps": steps,
    }
    if steps:
        out["phase"] = steps[0].get("phase", "")
    if step:
        out["step"] = step
    return out

