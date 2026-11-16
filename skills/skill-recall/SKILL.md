---
name: skill-recall
description: Measure whether installed agent skills actually fire when they should, using the user's real Claude Code session history. Use when the user says a skill "never triggers", "doesn't activate", "Claude ignores my skill", asks which skills are worth keeping, wants to improve a skill's description or triggering, wants real-prompt trigger evals instead of synthetic ones, or asks why /skill-doctor shows a skill as unused. Finds prompts where a skill was needed but not loaded, estimates per-skill recall, flags skills competing for the same prompts and descriptions dropped by the listing budget.
---

# skill-recall

`/skill-doctor` says which skills were never *used*. It can't say whether they were never *needed*. This skill finds the prompts in the user's own history where a skill should have fired and didn't, then helps fix the description using the user's real wording.

Everything goes through one stdlib-only script:

```bash
python3 <this-skill-dir>/scripts/skillrecall.py <command>
```

## How it works (so you can explain the numbers)

- **Sessions:** reads `~/.claude/projects/*/*.jsonl` and keeps real typed prompts. It drops tool results, compaction summaries, system notifications, subagent turns, eval-harness replays (the same one-prompt session 3+ times) and scripted `claude -p` runs.
- **Skills:** for each prompt, it only considers the skills **the model could see at that moment**, taken from the session's `skill_listing` records, so skills installed later never count as misses. It falls back to installed `SKILL.md` files when a session has no listing.
- **Candidates:** BM25 lexical match, length-normalized. These are noisy: in testing about 1 in 10 top candidates was a real miss.
- **Judge** (opt-in): a cheap model (`claude -p --model haiku`) confirms candidates in batches of 20, capped by `--judge-limit`. Verdicts are cached in `~/.cache/skill-recall/`, so each pair is only ever paid for once.
- **Est. recall** = prompts where the skill was used ÷ (those + confirmed misses), extrapolated with the judged precision. It's shown only for judged skills.

## Workflow

1. `report --redact` first. It's free and offline. Show the user the table, not the prompts.
2. If they want numbers they can trust: `report --judge --judge-limit 40`. That's 2 cheap-model calls, under a minute. Say that it sends the selected prompts and skill descriptions to the model.
3. For a skill with confirmed misses, read its "Top missed prompts" (without `--redact`, locally) and propose a description edit that adds **the user's own phrasings**: verbs, nouns, and Chinese terms if they write in Chinese. Keep it under the listing cap (~1,500 characters).
4. `export-evals --skill NAME --out evals.json` writes a `[{"query", "should_trigger"}]` set from real prompts, for skill-creator's description optimizer or `claude plugin eval`.
5. Check the **Desc. dropped** column. If it's non-zero, that skill's description was cut by the listing budget in those sessions. Suggest disabling unused skills, or raising `skillListingBudgetFraction`.
6. Check **Possible collisions**. Two skills matching the same prompts need sharper, non-overlapping descriptions, or one should go.

## Commands

| Command | Purpose |
|---|---|
| `skills` | Skills seen in recent sessions or installed, with description length |
| `report [--days N] [--skill X] [--judge] [--judge-limit N] [--redact] [--json] [--out F]` | The main report |
| `export-evals --skill X [--n 15] [--out F]` | Real-prompt trigger eval set |

Useful flags: `--exclude REGEX` skips sessions by working directory (e.g. eval workspaces). `--threshold` sets the raw BM25 floor (default 6).

## Rules

- The report contains the user's prompts. Never publish it or paste it anywhere outside this machine. Use `--redact` when sharing.
- Don't run `--judge` without the user's go-ahead. It uses their Claude quota and sends prompts to the model.
- Treat unjudged candidates as hypotheses, never as misses.
