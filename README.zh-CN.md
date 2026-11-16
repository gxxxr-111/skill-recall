# skill-recall

[English](README.md) | **简体中文**

> **状态：v0.1，实验性。** 支持 Claude Code 的会话记录（桌面端与 CLI）。记录格式没有官方文档，可能会变。

**你装的 skill 里，哪些该触发却没触发？**

"skill 该触发却不触发"在 Claude Code 的 issue 区反复出现：[#32184](https://github.com/anthropics/claude-code/issues/32184)（`claude -p` 下召回率为 0）、[#46952](https://github.com/anthropics/claude-code/issues/46952)（匹配看起来基于关键词）、[#14016](https://github.com/anthropics/claude-code/issues/14016)（子智能体里不会自动激活）、[#38588](https://github.com/anthropics/claude-code/issues/38588)（第一轮不调用）。这些 issue 大多因长期无人跟进被自动关闭，而不是被修复。现有工具各管一块：

| 工具 | 能告诉你什么 |
|---|---|
| `/skill-doctor`（内置） | 列表开销、每个 skill 的使用次数、从未调用的 skill |
| skill-creator / `claude plugin eval` | 在**人造**提示上的触发率，一次测一个 skill |
| skill-optimizer、spellbook | 给智能体用的审查清单，或给实际运行过的 skill 打分 |
| **skill-recall** | **你真实历史里需要某个 skill 却没加载它的那些提示**，以及每个 skill 的估计召回率 |

"从未调用"可能是"从没需要过"，也可能是"每次都错过了"。skill-recall 能把这两种情况分开。

## 它做什么

1. 读取本地会话记录，只保留你亲手输入的提示，剔除工具输出、上下文压缩摘要、评测脚本的重复回放和脚本驱动的 `claude -p` 调用。
2. 对每条提示，只考虑模型**在那一刻真正看到的** skill（来自会话里的 skill 列表记录），后来才装的 skill 不会被算成漏触发。
3. 用按长度归一的 BM25（英文单词 + 中文二元组）给"相关但没用上 skill"的（提示, skill）组合排序。
4. **可选**：用便宜的模型批量判定排名靠前的候选，判定结果会缓存，每一对只付费一次。
5. 按 skill 输出：使用次数、确认的漏触发、估计召回率、争抢同一批提示的 skill，以及因列表预算被截掉的描述。
6. 把真实提示导出成 `should_trigger` 评测集，供 skill-creator 或 `claude plugin eval` 使用。

## 安装

```
/plugin marketplace add gxxxr-111/skill-recall
/plugin install skill-recall@skill-recall
```

或者把 `skills/skill-recall` 复制到 `~/.claude/skills/`。需要 Python 3.10+，无依赖；`--judge` 需要 `claude` 命令行。

之后直接问 Claude"我的哪些 skill 没触发？"，或者手动运行：

```bash
S=~/.claude/skills/skill-recall/scripts/skillrecall.py
python3 $S report --redact                      # 免费、离线
python3 $S report --judge --judge-limit 40      # 约 2 次 Haiku 调用
python3 $S export-evals --skill my-skill --out evals.json
```

报告中的示例输出与解读见[英文 README](README.md#example-output-illustrative)。测试中，词汇匹配排名靠前的候选里只有约 1/10 是真正的漏触发，所以没经过判定的候选，报告一律不算作漏触发。

## 隐私

- 在本地运行。不加 `--judge` 时，没有任何数据离开你的电脑。
- `--judge` 会通过你自己的 `claude` 命令行，把选中的提示和 skill 描述发给模型。
- **报告里有你的提示原文**，分享前请加 `--redact`。

## 局限

- 目前只支持 Claude Code 的会话记录；其他智能体可以作为新的解析器加入。
- 会话内的 skill 列表记录已在桌面端会话上验证；没有这类记录的 CLI 会话，会退回到已安装的 `SKILL.md` 及其安装时间。
- 词汇预筛可能漏掉和描述毫无共同词的改写说法，所以估计的召回率是上限。
- 判定用的是小模型，请抽查确认的漏触发。

## 怎么做出来的

用 [look-before-you-build](https://github.com/gxxxr-111/look-before-you-build) 的 preflight skill 选题和设计，经过三轮元研究：候选空白 → 需求与已有方案 → 收窄。完整过程见 [`docs/preflight/`](docs/preflight/)，经验总结见 [`PREFLIGHT.md`](PREFLIGHT.md)。

## 测试

```bash
python3 -m unittest discover tests   # 离线，基于合成的会话记录
```

## 许可证

[MIT](LICENSE)。与 Anthropic 无关联。
