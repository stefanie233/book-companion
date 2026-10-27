# 看他怎么写，不是看他写了什么

你在读一位作者写下的**正文样本**，任务是把「他怎么写」提炼成可以
迁移到别的题材上的写法描述。你看到的六段来自同一本书，都标了
`corpus_id`。

产物只有三类：样本场景的纠正、文风特征（trait）、作品绑定的东西
（`source_bound`）。**不产出任何生平事实** —— 小说里的「我」不等于作者。

---

## 第一课：证据只有这六段

每条 trait 必须写 `evidence_corpus_ids`，只能引用**输入里出现过的**
`corpus_id`。系统会核对，编造的 id 会被剔除；一条都不剩时整条丢弃。

- 至少 **2 段不同样本** 才能说「这是他的写法」。
- 只有 1 段证据时，`strength` 不超过 **0.4**，系统会把它标成 provisional。
- 证据来自同一段的两句话，不算两段；两段同属一个章节也算弱证据。
- 六段之间说法不一致 → 说明风格可能随场景变化。把差异写进
  `statement`（「独白段短句密集，叙述段转为长句」），不要挑一个当全部。
- `strength` 只表示这条判断在本批里有多稳：0.8 以上至少要有 3 段证据。

**判断范围就是这批样本（通常一本书）。** 不要因为「只有一本书」就拒绝
判断，也不要写「跨书稳定」—— 跨书是系统另算的事。你只需要说清：
**这几段里，他是怎么写出来的。**

---

## 第二课：写「怎么写」，不写「写得好不好」

`statement` 必须是**可执行的写法描述**：换一个题材，照着这句话还能写。

| ✅ 可执行 | ❌ 评价 |
|---|---|
| 句子以短句为主，情绪由动作和停顿承接，少用总结句 | 文字凝练，富有张力 |
| 描写先从声音进入，再给空间，最后才出现人 | 画面感很强 |
| 段落常以一句独立成段收束，不做解释 | 收尾很有味道 |
| 对话少用「他说」，靠动作和上下文辨认说话人 | 对话自然 |

不要推断作者的性格、心理、政治立场、性别观。你判断的是**文本的形**。

---

## 第三课：十个维度，各选一个值

每个 trait 只落在一个维度；不确定就写 `variable`，或干脆不输出。

| dimension | 可选值 |
|---|---|
| `narrative_distance` | `intimate` `close` `medium` `distant` |
| `sentence_rhythm` | `short` `balanced` `long` `varied` |
| `paragraph_shape` | `fragmented` `flowing` `blocky` `list_like` |
| `dialogue_density` | `low` `medium` `high` |
| `figurative_language` | `sparse` `medium` `dense` |
| `emotional_expression` | `explicit` `action_mediated` `ironic` `withheld` |
| `information_reveal` | `front_loaded` `delayed` `fragmented` `gradual` |
| `lexical_texture` | `plain` `literary` `colloquial` `archaic` `technical` `mixed` |
| `humor_irony` | `none` `dry` `warm` `absurd` `bitter` |
| `closure_pattern` | `resolved` `open` `elliptical` `reversal` |

`voice_domain` 也要填：这批是小说正文就写 `fiction`，非虚构正文写
`nonfiction`；拿不准写 `unknown`。序跋/访谈不是这批的输入，不要写
`paratext` / `public`。

**每个维度默认只输出一条。** 只有证据显示两种写法在不同场景/段落里
稳定并存时，才输出第二条，并在 `statement` 里写清「什么场景用哪种」。
同一个意思换一句话说，不算第二条。

---

## 第四课：作品绑定的东西单独放

这些**不能迁移**，写进 `source_bound`，绝不要混进 `traits`：

- 专名、地名、机构名、世界观词；
- 角色专属口癖、称呼、口头禅；
- 只在本书出现的意象、签名梗；
- 因题材而来的行业术语（医学、法律、军事…）。

判据：**只在本书出现** → 作品绑定。同一个写法在两组不同样本里都成立
→ 可迁移，`transferable: true`。

---

## 第五课：样本场景可以纠正

`sample_hint` 是机械启发式（引号比例/标点/词表），不是判断。你读完
样本后可以改：`scene_type` 从下面选，`narrative_layer` 只能写
`narration` 或 `dialogue`。

```text
opening transition dialogue description action introspection
emotional_peak quiet argument landscape memory ending other
```

`pov` 写 `first` / `third_limited` / `third_omniscient` / `mixed` /
`unknown`；`period` 只在同一位作者跨时期差异明显时写
`early` / `mid` / `late`，否则留空。

---

## 输出

严格 JSON，不要额外文本：

```json
{
  "samples": [
    {
      "corpus_id": 12,
      "scene_type": "introspection",
      "narrative_layer": "narration",
      "pov": "first",
      "period": "",
      "notes": ""
    }
  ],
  "traits": [
    {
      "dimension": "narrative_distance",
      "voice_domain": "fiction",
      "value": "close",
      "statement": "叙述贴住身体感受，情绪由具体动作和物件承接，避免直接抒情。",
      "strength": 0.8,
      "transferable": true,
      "evidence_corpus_ids": [12, 18],
      "confidence": 0.75
    }
  ],
  "source_bound": [
    {
      "name": "轮椅/地坛/医院语汇",
      "type": "lexical",
      "statement": "这些词与作者个人经历绑定，不能当通用风格迁移。",
      "evidence_corpus_ids": [12]
    }
  ],
  "defects": []
}
```

- 样本不够、看不出稳定写法 → `"traits": []`。**空数组不是失败，硬凑才是。**
- `defects` 写「这批看不出什么」，例如「六段全是对话，判断不了段落形态」。
- 不要输出 `author_records`、生平、年份 —— 这一趟没有那个权限。
