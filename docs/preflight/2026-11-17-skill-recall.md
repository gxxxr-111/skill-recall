# Design brief: skill-recall

> Date: 2026-11-17 · Tier: 2 · Status: built (MVP), verified on a real history, published

## In one sentence

A local tool that reads your past agent sessions and tells you, with numbers, **which installed skills should have been used but weren't**, using your own real prompts, which can then be turned into test sets to fix the skill descriptions.

## How we got here (iterative meta-research)

1. **Round 1, first-hand evidence (no research cost):** five candidate gaps that had each cost real time or tokens in this session: cross-registry prior-art search, per-site access maps, skill trigger reliability, an owner-facing decision log, and skill security vetting.
2. **Round 2, demand and prior art** (two Sonnet subagents, every project named given a verdict):
   - **Trigger reliability ranked first.** Demand is strong: about 15 issues and posts, including Claude Code #32184 (0% recall in `claude -p`), #46952 (matching looks keyword-based) and #14016 (skills don't fire in subagents). Tooling is thin.
   - Security is crowded (NVIDIA SkillSpector, Snyk agent-scan, Cisco). Access maps have three commercial players and high legal risk. The decision log is clean but shallow. Registry search is crowded with weak tools.
3. **Round 3, narrowing:**
   - Anthropic's `/skill-doctor` already reports per-skill cost, usage and never-invoked skills, and the docs say description-budget overflow is logged.
   - skill-creator and `claude plugin eval` test made-up prompts on one skill at a time.
   - `skill-optimizer` (hqhq1025, 193★) has an agent checklist that mentions "undertrigger" but measures nothing. spellbook's `analyzing-skill-usage` scores the skills that *did* run.
   - **Nobody measures missed triggers on real usage.** "Never invoked" can't tell "not needed" from "needed and missed".
4. **Probe:** in a sample of recent real sessions, Skill calls were rare, and a custom skill fired only when explicitly requested. Real low recall, readable offline at zero token cost.

## The problem

- **The real need:** know whether the skills you installed actually get used when they should, and get the evidence needed to fix them, without paying for hundreds of synthetic eval runs.
- **How we'll know it works** (runnable checks):
  - Offline tests on synthetic transcripts. Known misses, invocations, slash commands and eval-run noise are classified correctly.
  - On a real history, the report runs in seconds with no network, and the candidate misses for a custom skill include requests it was written for.
  - With `--judge`, the cost stays bounded: about 20 pairs per cheap-model call, and a hard `--judge-limit`.
- **Out of scope**: rewriting descriptions automatically, live hooks that force skills to fire, security scanning.

## Recommended approach

A single stdlib-only Python CLI with these steps:
1. **Inventory** installed skills: user, project, plugin cache, and `~/.agents` / `~/.codex` skill folders.
2. **Parse** Claude Code transcripts into sessions: real user prompts, Skill tool calls, and slash commands. It drops tool results, meta and system turns, subagent turns, and eval-style repeated prompts.
3. **Score** every prompt against every skill with BM25. Latin words plus CJK bigrams, so Chinese prompts work too.
4. A (prompt, skill) pair is a **candidate miss** if it scores above a threshold and the skill wasn't used in the following turns of that session.
5. **Optionally judge** candidates in batches with a cheap model (`claude -p --model haiku`) to confirm them.
6. **Report** per skill: uses, candidate or confirmed misses, an estimated recall, the top missed prompts, and **collisions** (skill pairs competing for the same prompts).
7. **Export** a skill-creator / `plugin eval` trigger set built from real prompts.

## Key decisions

| Decision | Choice | Why | Rejected | Cost to change later |
|---|---|---|---|---|
| Matching | BM25 lexical + optional LLM judge | Free, offline and explainable first; precision added only where needed | Embeddings (need a model download or an API) | Low |
| Judge | `claude -p --model haiku`, batched, capped | Reuses the user's existing Claude auth; about 1/20 the calls of per-query evals | Per-pair calls; Opus | Low |
| Recall estimate | uses ÷ (uses + misses), labeled "estimate" | Honest about lexical noise | Claiming exact recall | Low |
| Agents | Claude Code transcripts now; parser seam for others | It's the format we can verify locally | Guessing Codex's format | Low |
| Output | Markdown report + JSON + eval export | People, agents and other tools can all use it | A web UI | Low |
| Privacy | Fully local by default; the judge is opt-in | Transcripts are private | Any hosted service | — |

## Risks

- **Lexical false positives** (prompts that mention words from a skill but don't need it): mitigated by the threshold, the session-window check and the optional judge. The report says "candidate" until something is confirmed.
- **The transcript format is undocumented** and could change: one parser function, and the tests pin the observed shape.
- **The report contains your prompts.** It's written locally, and the README warns not to publish it.

## Decisions for you

1. **Name:** no GitHub repo has the exact name `skill-recall`. Kept.
2. **Publish:** yes, as a public repo + plugin, with personal data removed.
3. **Codex support:** Codex stores sessions in SQLite. Deferred until there's demand.

## Change log

- 2026-11-17: initial version.
- 2026-11-17: verification on one real history (~140 sessions, ~1,000 typed prompts, ~35 skills).
  - Mid-build trigger: a premise turned out false. Disk inventory saw only 2 of the 37 skills the model was actually shown, so I switched to the transcripts' `skill_listing` records.
  - Fixed false misses: machine prompts (8 of 8 initial "misses"), uninstalled plugin caches, and skills installed mid-session (now handled by following the listing timeline).
  - Judge: 40 candidates, 2 Haiku calls, 44 s. Precision of the top lexical candidates was ~10%. Length normalization ranked true misses higher. Recall is now only shown for judged skills.
  - Finding: at least one built-in skill had zero uses and judge-confirmed misses, the exact case `/skill-doctor` reports as merely "unused".
