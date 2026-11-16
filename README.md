# skill-recall

**English** | [简体中文](README.zh-CN.md)

> **Status: v0.1, experimental.** Works on Claude Code transcripts (desktop app and CLI). The transcript format is undocumented and may change.

**Which of your agent skills should have fired, but didn't?**

Skills that don't trigger are one of the most common complaints about agent skills: [#32184](https://claudeissues.com/issue/32184-skill-auto-triggering-recall-0-in-headless-mode-claude-p-regardless-of-descripti), [#46952](https://claudeissues.com/issue/46952-bug-skill-auto-activation-appears-to-use-token-keyword-overlap-not-semantic-matc), [#14016](https://claudeissues.com/issue/14016-skills-dont-auto-activate-in-spawned-agents-via-task-tool) and many blog posts. The existing tools each measure a different part:

| Tool | What it tells you |
|---|---|
| `/skill-doctor` (built in) | Listing cost, how often each skill was used, skills never invoked |
| skill-creator / `claude plugin eval` | Trigger rate on **synthetic** prompts, one skill at a time |
| skill-optimizer, spellbook | Agent checklists for auditing skills, or scoring the ones that ran |
| **skill-recall** | **The prompts in your real history where a skill was needed and not loaded**, with an estimated recall per skill |

"Never invoked" can mean "never needed" or "missed every time". skill-recall separates the two.

## What it does

1. Reads your local session transcripts and keeps only prompts you typed. It drops tool output, compaction summaries, eval-harness replays and scripted `claude -p` runs.
2. For each prompt, it considers only the skills the model was **actually shown at that moment**, taken from the session's skill listing. A skill installed later never counts as a miss.
3. It ranks (prompt, skill) pairs by length-normalized BM25 (Latin words + CJK bigrams) where the skill **wasn't** used.
4. **Optional:** a cheap model judges the top candidates in batches. Verdicts are cached, so each pair is paid for once.
5. It reports per-skill uses, confirmed misses, estimated recall, skills competing for the same prompts, and descriptions dropped by the listing budget.
6. It exports real prompts as a `should_trigger` eval set for skill-creator or `claude plugin eval`.

## Install

```
/plugin marketplace add gxxxr-111/skill-recall
/plugin install skill-recall@skill-recall
```

Or copy `skills/skill-recall` into `~/.claude/skills/`. Python 3.10+, no dependencies. The `--judge` option needs the `claude` CLI.

Then ask Claude *"which of my skills aren't triggering?"*, or run it directly:

```bash
S=~/.claude/skills/skill-recall/scripts/skillrecall.py
python3 $S report --redact                      # free, offline
python3 $S report --judge --judge-limit 40      # ~2 Haiku calls
python3 $S export-evals --skill my-skill --out evals.json
```

## Example output (illustrative)

```
Sessions: 140 · prompts: 1000 · skills seen: 35 · dropped as eval-like: 400, scripted: 110

| Skill           | Uses | Candidates | Judged (confirmed) | Est. recall |
| research-helper |    0 |         25 |            20 (3)  |         0%  |
| docx            |    4 |          2 |             2 (1)  |        80%  |
| code-review     |    0 |         15 |            10 (0)  |          —  |
```

How to read it: `research-helper` was never invoked even though the judge confirmed requests it was built for, so its description needs the user's own wording. `code-review` had many word-overlap candidates, but the judge rejected all of them. In testing, only about 1 in 10 top lexical candidates was a real miss, which is why the report never calls an unjudged candidate a miss.

## Privacy

- Runs locally. Without `--judge`, nothing leaves your machine.
- `--judge` sends the selected prompts and skill descriptions to the model through your own `claude` CLI.
- **The report contains your prompts.** Use `--redact` before sharing it.

## Limits

- Claude Code transcripts only, for now. Other agents can be added as extra parsers.
- The per-session skill listing was verified on desktop-app sessions. For CLI sessions without it, the tool falls back to installed `SKILL.md` files plus their install time.
- Lexical prefiltering can miss paraphrases that share no words with the description. Recall estimates are an upper bound on how well the skill does.
- Judged by a small model. Spot-check the confirmed misses.

## How it was built

Chosen and designed with the [look-before-you-build](https://github.com/gxxxr-111/look-before-you-build) preflight skill, through three rounds of meta-research: candidate gaps, then demand and prior art, then narrowing. The full trail is in [`docs/preflight/`](docs/preflight/), and lessons learned are in [`PREFLIGHT.md`](PREFLIGHT.md).

## Tests

```bash
python3 -m unittest discover tests   # offline, synthetic transcripts
```

## License

[MIT](LICENSE). Not affiliated with Anthropic.
