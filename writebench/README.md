# writebench — 写作能力评测台

对标 agent-lightning 的 **Skill 循环**：固定用例 → 改一个可解释的变量 →
重复测量 → 只保留有证据的改动。这里不做梯度训练（闭源 API + 本机无可用
GPU），只做 harness 层优化。

## 为什么是这套指标

- **机械指标**：字数、视角、合同事件命中、禁令、参考素材泄漏、复制率、
  套话阻塞项。确定性、零成本、可复现。
- **成对盲评**：同一用例的 A/B 两稿交换位置各判一次，不一致记平局。
  比绝对打分稳，能抵消裁判的位置偏好与分数漂移。
- **一致性裁判**：只用于续写用例，检查事实矛盾、知识越界、承诺提前兑现。

## 用例

| id | 类型 | 说明 |
|----|------|------|
| c01_old_house | 冷启动 | 回忆散文，第一人称，克制 |
| c02_tea_house | 冷启动 | 茶馆密谈，信息控制 |
| c03_night_shift | 冷启动 | 夜班门卫独白，动机留白 |
| c04_city_memory | 冷启动 | 地图/领土随笔，概念落地 |
| c05_ferry_ch3 | 续写 | 第 3 章，接两章手稿与账本 |
| c06_long_night_ch3 | 续写 | 第 3 章，接两章手稿与账本 |

splits：`train` = c01/c02/c06，`dev` = c03/c04/c05。所有配方都在这 6 个
用例上跑；split 只用于记录，**没有据 dev 结果回头改提示词以外的选择**。

## 配方（一轮只改一个变量）

- `base_pov`：生产提示词 + 视角注入（视角修复前的 base 会把第一人称写成第三人称）。
- `ref_raw`：原文摘录直接进上下文。
- `ref_card`：蒸馏卡（写法 + 细节 + 句式），首轮版本。
- `ref_card_v2`：只给观察方式与细节，去掉叙事策略与完整对白。
- `ref_card_v2_len`：v2 卡 + 长度纪律（目标/下限 + 补细节而非总结）。
- `ref_card_v2_repair`：v2 卡 + 机械质检后的单轮修订。
- `prod`：逐字复刻生产 system prompt（素材说明在前，长度纪律最后）。
- `ref_hybrid_v2_len`：混合召回（向量 + BM25）过 v2 蒸馏，再接长度纪律。
- `voice` / `ref_card_voice2`：AuthorGraph 声音透镜。

## 召回评测

`writebench/recall/` 是独立的检索评测台：9.5k 单元索引、30 条合成查询、
6 个真实用例，比较 BM25 / 向量 / 混合。结论与跑法见
[`recall/README.md`](recall/README.md)。

## 运行

```powershell
$env:PYTHONIOENCODING="utf-8"
python -m writebench.run --recipes ref_card_v2_len `
  --cases c01_old_house,c02_tea_house,c03_night_shift,c04_city_memory,`
c05_ferry_ch3,c06_long_night_ch3 --tag r3len --concurrency 3 --no-judge
python -m writebench.compare --a base_pov --a-tag r1b,cont1 `
  --b ref_card_v2_len --b-tag r3len
```

产物在 `C:\tmp\wb_runs\<tag>\<recipe>\<case>__<recipe>__r<rep>\`：
`chapter.txt`、`output.json`（含装配文本、素材、机械指标）。
蒸馏卡缓存 `C:\tmp\wb_runs\_material\`。

## 已测结果（单次重复，方向性结论）

| 对比 | 结果 | 结论 |
|------|------|------|
| base vs base_pov | — | 视角 bug 真实存在，已修 |
| base_pov vs ref_raw | raw 机械分更低、字数更差 | 原文直灌是负资产 |
| base_pov vs ref_card（v1） | 冷启动 2:2，续写 2:0 卡胜 | 卡有用，但首章会被写法建议带偏 |
| base_pov vs ref_card_v2 | **4:2 v2 胜**（四个冷启动全胜） | 去掉策略、只给细节后，首章不再被带偏 |
| base_pov vs ref_card_v2_len | **6:0 v2_len 胜**（含两个续写） | 细节卡 + 长度纪律是当前最优生产配置 |
| base_pov vs prod（精确复刻生产 prompt） | **5:1 prod 胜** | 生产配置真实验证通过 |
| ref_card_v2_len vs prod | **3:3** | 两种写法等价；证明生产 prompt 没跑偏 |
| ref_card_v2 vs v2_len | **3:1 v2_len 胜** | 长度纪律补回具体动作 |
| ref_card_len vs v2_len | **5:1 v2_len 胜** | v1 的“信息给法”建议会诱发合同违规 |
| v2_len vs v2_repair | **5:1 v2_len 胜** | 按质检单扩写会改信息给法、提前揭底；自动修订默认关 |
| ref_card vs ref_card+voice | **2:0 ref_card 胜** | 当前声音透镜会让句式更用力、概念更直白 |

## 局限

- 每格目前 1 次重复；裁判是 LLM，平局与抖动都存在。最终结论要跑 2–3 次重复。
- 机械字数仍会掉到 0.64–0.71（目标 1800/2000）；裁判偏好长稿，但合同字数
  是硬指标。自动修订能补字数，却会改信息给法（1:5），所以生产默认关。
- 裁判只看文本，不看生成耗时与 token 成本。doubao-seed-2.1-pro 在 6 用例上
  平均约 411s/章（3 并发）；素材卡另有一次蒸馏调用（按章+合同缓存）。
- `must_hit_keywords` 是关键词代理，会漏判同义写法（c01 写「母亲的房门」但没写
  「房间」；c04 写了迷路过程但没写「迷路」二字）。判读时先看原文再信这个数。
