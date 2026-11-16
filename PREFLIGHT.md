# PREFLIGHT notes: skill-recall

- **Most "user" prompts in transcripts aren't typed by a person.** Before scoring anything, filter compaction summaries (`isCompactSummary`), system notifications (`promptSource: system`), tool results, subagent turns, eval-harness replays (same one-prompt session 3+ times) and scripted `claude -p` sessions (first prompt like "You are optimizing…"). In testing, every one of the first batch of "misses" was a machine prompt.
- **Availability must follow the timeline.** A skill installed mid-session is not a miss for earlier prompts. Use the session's `skill_listing` attachments in order, not a union and not file birth times.
- **The model's real skill list is in the transcript.** `skill_listing` attachments include app-served skills that aren't on disk, and names without a description line show budget-dropped descriptions.
- **Lexical matching is a prefilter, not a verdict.** Judged precision of the top lexical candidates was ~10%. Length-normalizing (score ÷ √query terms) moved true misses to the top, but only a judge makes the numbers trustworthy. Never print recall for unjudged skills.
