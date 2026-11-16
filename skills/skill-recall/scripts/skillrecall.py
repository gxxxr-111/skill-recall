#!/usr/bin/env python3
"""skill-recall: find the moments your agent should have used a skill but didn't.

Reads local Claude Code transcripts (~/.claude/projects/*/*.jsonl) and installed
SKILL.md files, scores every real user prompt against every skill (BM25, Latin
words + CJK bigrams), and reports per-skill uses, candidate misses, an estimated
recall, and skills that compete for the same prompts. An optional cheap-model
judge confirms candidates. Standard library only; nothing leaves the machine
unless you pass --judge.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
CACHE = Path(os.environ.get("SKILL_RECALL_CACHE", HOME / ".cache" / "skill-recall"))
JUDGE_CWD = CACHE / "judge-workdir"

STOP = set("""a an the and or but if then else of to in on at by for with from as is are was
were be been being it its this that these those there here what which who whom whose
when where why how all any both each few more most other some such no nor not only own
same so than too very can will just should now do does did doing done have has had i me
my we our you your he she they them their his her us also into about over under again
further once up down out off am get got make made let need want please help could would
use used using skill skills user users agent claude task tasks thing things something like
one two way ok okay yes thanks hi hey via e.g etc even still may might must shall
""".split())
CJK = re.compile(r"[㐀-鿿豈-﫿]+")
WORD = re.compile(r"[a-z][a-z0-9+#]*(?:[-_.][a-z0-9+#]+)*")
CMD = re.compile(r"<command-name>/?([^<\s]+)</command-name>")
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
PROGRAMMATIC = re.compile(r"^\s*(You are (an?|the|\w+ing)\b|Your task is|# ?Task\b)", re.I)
TAGGED = re.compile(r"^\s*<(local-command|command-|bash-|task-notification|user-prompt-submit)")


# ---------------------------------------------------------------- text

def stem(w: str) -> str:
    for suf in ("ing", "ers", "er", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def tokens(text: str) -> list[str]:
    text = text.lower()
    out = [stem(w) for w in WORD.findall(text) if w not in STOP and len(w) > 1]
    for run in CJK.findall(text):
        out += [run[i:i + 2] for i in range(len(run) - 1)] or [run]
    return out


# ---------------------------------------------------------------- skills

@dataclass
class Skill:
    name: str            # as invoked, e.g. "preflight" or "plugin:skill"
    description: str
    source: str          # user / plugin / project / other
    path: str
    installed: float = 0.0   # epoch seconds; prompts before this can't be misses
    terms: collections.Counter = field(default_factory=collections.Counter)

    @property
    def short(self) -> str:
        return self.name.split(":")[-1]


def parse_frontmatter(text: str) -> dict:
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    out, key, block = {}, None, []
    for line in m.group(1).splitlines():
        km = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if km and not line.startswith(" "):
            if key and block:
                out[key] = " ".join(block).strip()
            key, val = km.group(1), km.group(2).strip()
            block = []
            if val in (">", "|", ">-", "|-"):
                continue
            out[key] = val.strip("\"'")
            key = key if val == "" else None
        elif key:
            block.append(line.strip())
    if key and block:
        out[key] = " ".join(block).strip()
    return out


def installed_plugin_paths(path: Path | None = None) -> list[tuple[str, str]]:
    path = path or HOME / ".claude/plugins/installed_plugins.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for key, installs in (data.get("plugins") or {}).items():
        for inst in installs if isinstance(installs, list) else [installs]:
            if isinstance(inst, dict) and inst.get("installPath"):
                out.append((key.split("@")[0], inst["installPath"]))
    return out


def discover_skills(extra_dirs: list[str] | None = None) -> list[Skill]:
    found: dict[str, tuple[float, Skill]] = {}

    def add(path: str, source: str, prefix: str = ""):
        try:
            fm = parse_frontmatter(Path(path).read_text(errors="ignore"))
        except OSError:
            return
        name = fm.get("name") or Path(path).parent.name
        full = f"{prefix}:{name}" if prefix else name
        desc = " ".join(filter(None, [fm.get("description", ""), fm.get("when_to_use", "")]))
        mtime = os.path.getmtime(path)
        st = os.stat(Path(path).parent)
        born = getattr(st, "st_birthtime", None) or st.st_ctime
        if full not in found or mtime > found[full][0]:
            found[full] = (mtime, Skill(full, desc, source, path, installed=born))

    for p in glob.glob(str(HOME / ".claude/skills/*/SKILL.md")):
        add(p, "user")
    # Only plugins that are actually installed; the cache keeps uninstalled ones too.
    for plugin, path in installed_plugin_paths():
        for p in glob.glob(str(Path(path) / "skills/*/SKILL.md")):
            add(p, "plugin", prefix=plugin)
    for base in (HOME / ".agents/skills", HOME / ".codex/skills"):
        for p in glob.glob(str(base / "*/SKILL.md")):
            add(p, "other")
    for d in extra_dirs or []:
        for p in glob.glob(str(Path(d).expanduser() / "*/SKILL.md")):
            add(p, "project")
    skills = [s for _, s in found.values()]
    for s in skills:
        s.terms = collections.Counter(tokens(s.description) + tokens(s.short.replace("-", " ")) * 2)
    return sorted(skills, key=lambda s: s.name)


def merge_inventory(disk: list[Skill], sessions: list["Session"]) -> list[Skill]:
    """Skills the model was actually shown (from session listings) plus installed ones on disk."""
    by_name = {s.name: s for s in disk}
    for s in sorted(sessions, key=lambda s: s.events[0].ts if s.events else ""):
        for name, desc in s.listed.items():
            sk = by_name.get(name) or next((d for d in disk if same_skill(name, d)), None)
            if sk is None:
                sk = by_name[name] = Skill(name, desc, "listed", "")
            if desc:
                sk.description = desc   # latest wording the model saw
    for sk in by_name.values():
        sk.terms = collections.Counter(tokens(sk.description) + tokens(sk.short.replace("-", " ")) * 2)
    return sorted(by_name.values(), key=lambda s: s.name)


def same_skill(invoked: str, skill: Skill) -> bool:
    inv = invoked.lstrip("/")
    return inv == skill.name or inv.split(":")[-1] == skill.short


# ---------------------------------------------------------------- transcripts

@dataclass
class Event:
    kind: str     # "prompt" | "skill" | "listing"
    text: str
    ts: str = ""


@dataclass
class Session:
    sid: str
    cwd: str
    events: list[Event]
    listed: dict[str, str] = field(default_factory=dict)   # skill -> description the model saw
    dropped: set[str] = field(default_factory=set)         # listed by name only (budget overflow)

    @property
    def prompts(self) -> list[Event]:
        return [e for e in self.events if e.kind == "prompt"]


def prompt_text(content) -> str:
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    else:
        return ""
    return REMINDER.sub("", text).strip()


LISTING_LINE = re.compile(r"^- (\S+?):\s+(.+)$")


def parse_listing(att: dict) -> tuple[dict[str, str], set[str]]:
    """A `skill_listing` attachment: the skills (and descriptions) the model was shown."""
    listed = {}
    for line in str(att.get("content", "")).splitlines():
        m = LISTING_LINE.match(line.strip())
        if m:
            listed[m.group(1)] = m.group(2).strip()
    names = [n for n in att.get("names") or [] if isinstance(n, str)]
    dropped = {n for n in names if n not in listed}
    for n in dropped:
        listed[n] = ""
    return listed, dropped


def parse_transcript(lines) -> Session | None:
    events, sid, cwd = [], "", ""
    listed, dropped = {}, set()
    for line in lines:
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if d.get("isSidechain"):
            continue
        sid = sid or d.get("sessionId", "")
        cwd = cwd or d.get("cwd", "")
        att = d.get("attachment")
        if d.get("type") == "attachment" and isinstance(att, dict) and att.get("type") == "skill_listing":
            l, dr = parse_listing(att)
            for k, v in l.items():
                if v or k not in listed:
                    listed[k] = v
            dropped |= dr
            events.append(Event("listing", json.dumps(sorted(l)), d.get("timestamp", "")))
            continue
        msg = d.get("message") or {}
        if d.get("type") == "assistant" and isinstance(msg.get("content"), list):
            for b in msg["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill":
                    events.append(Event("skill", str((b.get("input") or {}).get("skill", "")), d.get("timestamp", "")))
        elif d.get("type") == "user":
            if (d.get("toolUseResult") or d.get("isMeta") or d.get("isCompactSummary")
                    or d.get("promptSource") == "system"):
                continue
            text = prompt_text(msg.get("content"))
            cm = CMD.search(text)
            if cm:  # a slash command: the user invoked it directly
                events.append(Event("skill", cm.group(1), d.get("timestamp", "")))
                continue
            if len(text) < 4 or TAGGED.match(text) or text.startswith(("[Request interrupted", "Caveat:")):
                continue
            events.append(Event("prompt", text, d.get("timestamp", "")))
    dropped -= {k for k, v in listed.items() if v}
    has_prompt = any(e.kind == "prompt" or e.kind == "skill" for e in events)
    return Session(sid, cwd, events, listed, dropped) if has_prompt else None


def load_sessions(projects_dir: Path, days: int | None, exclude: str | None) -> tuple[list[Session], dict]:
    cutoff = None if not days else dt.datetime.now().timestamp() - days * 86400
    ex = re.compile(exclude) if exclude else None
    sessions, stats = [], collections.Counter()
    for f in glob.glob(str(projects_dir / "*" / "*.jsonl")):
        if cutoff and os.path.getmtime(f) < cutoff:
            continue
        with open(f, errors="ignore") as fh:
            s = parse_transcript(fh)
        if not s:
            continue
        if s.cwd.startswith(str(JUDGE_CWD)) or (ex and ex.search(s.cwd)):
            stats["excluded"] += 1
            continue
        sessions.append(s)
    # Eval harnesses replay the same one-prompt session many times; drop that noise.
    firsts = collections.Counter(s.prompts[0].text for s in sessions if len(s.prompts) == 1)
    kept = []
    for s in sessions:
        one = s.prompts[0].text if len(s.prompts) == 1 else None
        if one is not None and firsts[one] >= 3:
            stats["eval_like"] += 1
        elif s.prompts and PROGRAMMATIC.match(s.prompts[0].text):
            stats["programmatic"] += 1   # a script driving `claude -p`, not a person
        else:
            kept.append(s)
    stats["sessions"] = len(kept)
    stats["prompts"] = sum(len(s.prompts) for s in kept)
    return kept, dict(stats)


# ---------------------------------------------------------------- scoring

class Scorer:
    """BM25 with skills as documents and each prompt as the query."""

    def __init__(self, skills: list[Skill], k1=1.2, b=0.75):
        self.skills, self.k1, self.b = skills, k1, b
        n = max(len(skills), 1)
        df = collections.Counter(t for s in skills for t in s.terms)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        self.avg = sum(sum(s.terms.values()) for s in skills) / n or 1

    def score(self, prompt: str, skill: Skill) -> tuple[float, list[str], float]:
        """Raw BM25, matched terms, and a length-normalised score (raw / sqrt(query terms))."""
        q = set(tokens(prompt[:2000]))
        dl = sum(skill.terms.values()) or 1
        total, hit = 0.0, []
        for t in q:
            tf = skill.terms.get(t, 0)
            if tf:
                total += self.idf[t] * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * dl / self.avg))
                hit.append(t)
        return total, sorted(hit, key=lambda t: -self.idf[t]), total / math.sqrt(len(q) or 1)


@dataclass
class Pair:
    skill: str
    prompt: str
    score: float
    terms: list[str]
    session: str
    used: bool
    norm: float = 0.0
    verdict: bool | None = None

    @property
    def key(self) -> str:
        return hashlib.sha1(f"{self.skill}\0{self.prompt}".encode()).hexdigest()[:16]


def ts_epoch(ts: str) -> float:
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def analyze(sessions: list[Session], skills: list[Skill], threshold: float, window: int = 2,
            min_terms: int = 2, min_norm: float = 1.5):
    scorer = Scorer(skills)
    uses = collections.Counter()
    used_prompts = collections.Counter()
    pairs: list[Pair] = []
    seen: dict[tuple[str, str], Pair] = {}
    dropped = collections.Counter()
    for s in sessions:
        ev = s.events
        for sk in skills:
            if any(same_skill(n, sk) for n in s.dropped):
                dropped[sk.name] += 1
        for e in ev:
            if e.kind == "skill":
                for sk in skills:
                    if same_skill(e.text, sk):
                        uses[sk.name] += 1
        listings = [set(json.loads(e.text)) for e in ev if e.kind == "listing"]
        visible = set(listings[0]) if listings else None  # prompts before any listing see the first
        prompt_idx = [i for i, e in enumerate(ev) if e.kind == "prompt"]
        pos = {i: n for n, i in enumerate(prompt_idx)}
        for i, e in enumerate(ev):
            if e.kind == "listing":
                visible = (visible or set()) | set(json.loads(e.text))
                continue
            if e.kind != "prompt":
                continue
            n = pos[i]
            end = prompt_idx[n + window] if n + window < len(prompt_idx) else len(ev)
            active = {x.text for x in ev[:end] if x.kind == "skill"}
            soon = {x.text for x in ev[i:end] if x.kind == "skill"}
            pts = ts_epoch(e.ts)
            if visible is not None:   # only skills the model could actually see at this moment
                avail = [sk for sk in skills if any(same_skill(v, sk) for v in visible)]
            else:
                avail = [sk for sk in skills if not (pts and sk.installed and pts < sk.installed)]
            for sk in avail:
                if any(same_skill(x, sk) for x in soon):
                    used_prompts[sk.name] += 1
                sc, hit, norm = scorer.score(e.text, sk)
                if sc >= threshold and norm >= min_norm and len(hit) >= min_terms:
                    used = any(same_skill(x, sk) for x in active)
                    k = (sk.name, e.text)
                    if k in seen:  # the same prompt repeated: count it once, used if ever used
                        seen[k].used |= used
                        continue
                    seen[k] = Pair(sk.name, e.text, round(sc, 2), hit[:6], s.sid, used, round(norm, 2))
                    pairs.append(seen[k])
    return pairs, uses, used_prompts, dropped


def collisions(pairs: list[Pair], min_shared: int = 3, min_jaccard: float = 0.3):
    by_skill = collections.defaultdict(set)
    for p in pairs:
        by_skill[p.skill].add(p.prompt)
    names, out = sorted(by_skill), []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared = by_skill[a] & by_skill[b]
            if len(shared) >= min_shared:
                j = len(shared) / len(by_skill[a] | by_skill[b])
                if j >= min_jaccard:
                    out.append((a, b, len(shared), round(j, 2)))
    return sorted(out, key=lambda x: -x[3])


# ---------------------------------------------------------------- judge

def load_verdicts() -> dict:
    p = CACHE / "verdicts.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_verdicts(v: dict):
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / "verdicts.json").write_text(json.dumps(v))


JUDGE_PROMPT = """You are auditing an AI coding agent's skill use. Each item gives a skill's
description and a real user request. Answer true only if the request clearly falls
within what the skill describes, so a careful agent should have loaded that skill
for this request. Answer false if the overlap is only a shared word or topic.
Reply with ONLY a JSON array of booleans, one per item, in order.

Items:
"""


def run_judge(items: list[dict], model: str) -> list[bool] | None:
    JUDGE_CWD.mkdir(parents=True, exist_ok=True)
    prompt = JUDGE_PROMPT + json.dumps(items, ensure_ascii=False, indent=0)
    try:
        r = subprocess.run(["claude", "-p", "--model", model], input=prompt, text=True,
                           capture_output=True, timeout=300, cwd=JUDGE_CWD)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"judge call failed: {e}", file=sys.stderr)
        return None
    m = re.search(r"\[[\s\S]*?\]", r.stdout)
    try:
        out = json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        out = None
    if not isinstance(out, list) or len(out) != len(items):
        print(f"judge returned unusable output: {r.stdout[:200]!r} {r.stderr[:200]!r}", file=sys.stderr)
        return None
    return [bool(x) for x in out]


def judge_pairs(pairs: list[Pair], skills: dict[str, Skill], model: str, limit: int,
                batch: int = 20, runner=run_judge) -> int:
    cache = load_verdicts()
    todo = []
    for p in pairs:
        if p.key in cache:
            p.verdict = cache[p.key]
        elif not p.used:
            todo.append(p)
    todo.sort(key=lambda p: -p.norm)
    todo = todo[:limit]
    calls = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        items = [{"skill": p.skill, "description": skills[p.skill].description[:700],
                  "request": p.prompt[:900]} for p in chunk]
        res = runner(items, model)
        calls += 1
        if res is None:
            continue
        for p, v in zip(chunk, res):
            p.verdict = cache[p.key] = v
        save_verdicts(cache)
    return calls


# ---------------------------------------------------------------- report

def summarize(skills, pairs, uses, used_prompts, dropped=None):
    """Recall is only estimated where the judge has confirmed or rejected candidates."""
    dropped = dropped or {}
    rows = []
    for sk in skills:
        ps = [p for p in pairs if p.skill == sk.name and not p.used]
        judged = [p for p in ps if p.verdict is not None]
        confirmed = sum(1 for p in judged if p.verdict)
        u = used_prompts[sk.name]
        misses = recall = None
        if judged:  # extrapolate the judged precision to this skill's unjudged candidates
            misses = confirmed + (len(ps) - len(judged)) * confirmed / len(judged)
            recall = u / (u + misses) if (u + misses) else None
        elif not ps:
            misses = 0.0
            recall = 1.0 if u else None
        rows.append({"skill": sk.name, "source": sk.source, "uses": uses[sk.name],
                     "used_after_prompts": u, "candidates": len(ps), "judged": len(judged),
                     "confirmed": confirmed,
                     "est_misses": None if misses is None else round(misses, 1),
                     "est_recall": None if recall is None else round(recall, 2),
                     "desc_dropped_sessions": dropped.get(sk.name, 0),
                     "desc_chars": len(sk.description)})
    return sorted(rows, key=lambda r: (-(r["est_misses"] or 0), -r["candidates"], -r["uses"], r["skill"]))


def clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def render(rows, pairs, cols, stats, args) -> str:
    L = ["# skill-recall report", "",
         f"Sessions: {stats.get('sessions', 0)} · prompts: {stats.get('prompts', 0)} · "
         f"skills seen: {stats.get('skills', 0)} · dropped as eval-like: {stats.get('eval_like', 0)}, "
         f"scripted: {stats.get('programmatic', 0)}, excluded: {stats.get('excluded', 0)} · "
         f"threshold: {args.threshold}" + (f" · last {args.days} days" if args.days else ""), "",
         "**Candidates** are prompts that look lexically related to a skill that wasn't used; most "
         "are noise until judged. **Est. recall** = prompts where the skill was used ÷ (those + "
         "confirmed misses), shown only for judged skills (run with `--judge`).", "",
         "| Skill | Source | Uses | Candidate misses | Judged (confirmed) | Est. recall | Desc. dropped |",
         "|---|---|---|---|---|---|---|"]
    active = [r for r in rows if r["uses"] or r["candidates"] or r["desc_dropped_sessions"]]
    for r in active:
        rec = "—" if r["est_recall"] is None else f"{r['est_recall']:.0%}"
        jd = f"{r['judged']} ({r['confirmed']})" if r["judged"] else "—"
        dd = f"{r['desc_dropped_sessions']} sessions" if r["desc_dropped_sessions"] else ""
        L.append(f"| {r['skill']} | {r['source']} | {r['uses']} | {r['candidates']} | {jd} | {rec} | {dd} |")
    quiet = len(rows) - len(active)
    if quiet:
        L += ["", f"{quiet} other skills had no uses and no candidate misses."]
    L += ["", "## Top missed prompts", ""]
    for r in rows:
        ps = [p for p in pairs if p.skill == r["skill"] and not p.used and p.verdict is not False]
        if not ps:
            continue
        ps.sort(key=lambda p: (p.verdict is not True, -p.norm))
        L.append(f"### {r['skill']}")
        for p in ps[: args.top]:
            tag = "✔ confirmed miss" if p.verdict else f"unverified, score {p.norm}"
            text = "(redacted)" if args.redact else clip(p.prompt, 140)
            L.append(f"- {tag} · matched: {', '.join(p.terms[:4])} · {text}")
        L.append("")
    if cols:
        L += ["## Possible collisions (skills matching the same prompts)", "",
              "| Skill A | Skill B | Shared prompts | Jaccard |", "|---|---|---|---|"]
        L += [f"| {a} | {b} | {n} | {j} |" for a, b, n, j in cols]
        L.append("")
    L += ["## Next steps", "",
          "- Add the user's own wording from confirmed misses to the skill's `description`.",
          "- `export-evals --skill NAME` turns these real prompts into a skill-creator / plugin-eval trigger set.",
          "- This report contains your prompts. Don't publish it."]
    return "\n".join(L) + "\n"


def export_evals(pairs, skill: str, n: int, seed: int = 0) -> list[dict]:
    rng = random.Random(seed)
    pos = [p for p in pairs if p.skill == skill and (p.used or p.verdict)]
    neg = [p for p in pairs if p.skill == skill and p.verdict is False]
    pos.sort(key=lambda p: -p.norm)
    out = [{"query": p.prompt[:1500], "should_trigger": True} for p in pos[:n]]
    rng.shuffle(neg)
    out += [{"query": p.prompt[:1500], "should_trigger": False} for p in neg[:n]]
    return out


# ---------------------------------------------------------------- CLI

def build(args):
    sessions, stats = load_sessions(Path(args.projects_dir).expanduser(), args.days, args.exclude)
    pool = merge_inventory(discover_skills(args.skills_dir), sessions)  # IDF over the full set
    skills = pool
    if args.skill:
        skills = [s for s in pool if args.skill in (s.name, s.short)]
        if not skills:
            sys.exit(f"no skill named {args.skill!r}; see `skills`")
    pairs, uses, used_prompts, dropped = analyze(sessions, pool, args.threshold)
    if args.skill:
        names = {s.name for s in skills}
        pairs = [p for p in pairs if p.skill in names]
    calls = 0
    if getattr(args, "judge", False):
        calls = judge_pairs(pairs, {s.name: s for s in pool}, args.model, args.judge_limit)
    else:
        cache = load_verdicts()
        for p in pairs:
            p.verdict = cache.get(p.key)
    stats.update(judge_calls=calls, skills=len(pool))
    return skills, pairs, uses, used_prompts, dropped, stats


def cmd_skills(args):
    sessions, _ = load_sessions(Path(args.projects_dir).expanduser(), args.days, None)
    for s in merge_inventory(discover_skills(args.skills_dir), sessions):
        print(f"{s.source:7} {len(s.description):5}ch  {s.name}")


def cmd_report(args):
    skills, pairs, uses, used_prompts, dropped, stats = build(args)
    rows = summarize(skills, pairs, uses, used_prompts, dropped)
    cols = [] if args.skill else collisions(pairs)
    if args.json:
        out = json.dumps({"stats": stats, "skills": rows, "collisions": cols}, ensure_ascii=False, indent=1)
    else:
        out = render(rows, pairs, cols, stats, args)
    if args.out:
        Path(args.out).write_text(out)
        print(f"wrote {args.out} ({stats.get('judge_calls', 0)} judge calls)")
    else:
        sys.stdout.write(out)


def cmd_export(args):
    skills, pairs, *_ = build(args)
    data = export_evals(pairs, skills[0].name, args.n)
    text = json.dumps(data, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {len(data)} queries to {args.out}")
    else:
        print(text)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="skillrecall", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--projects-dir", default=str(HOME / ".claude/projects"))
        p.add_argument("--skills-dir", action="append", help="extra folder of skills (repeatable)")
        p.add_argument("--days", type=int, help="only sessions modified in the last N days")
        p.add_argument("--exclude", help="regex on session cwd to skip (e.g. eval workspaces)")
        p.add_argument("--threshold", type=float, default=6.0, help="BM25 score for a candidate (default 6)")
        p.add_argument("--skill", help="limit to one skill")

    p = sub.add_parser("skills", help="list skills seen in sessions or installed")
    p.add_argument("--skills-dir", action="append")
    p.add_argument("--projects-dir", default=str(HOME / ".claude/projects"))
    p.add_argument("--days", type=int, default=30)
    p = sub.add_parser("report", help="per-skill uses, misses, recall, collisions")
    common(p)
    p.add_argument("--judge", action="store_true", help="confirm candidates with a cheap model via `claude -p`")
    p.add_argument("--judge-limit", type=int, default=60, help="max candidates to judge (default 60)")
    p.add_argument("--model", default="haiku")
    p.add_argument("--top", type=int, default=5, help="missed prompts to show per skill")
    p.add_argument("--redact", action="store_true", help="hide prompt text in the report")
    p.add_argument("--json", action="store_true")
    p.add_argument("--out")
    p = sub.add_parser("export-evals", help="real prompts as a should_trigger eval set")
    common(p)
    p.add_argument("--n", type=int, default=15, help="max positives and negatives each")
    p.add_argument("--out")
    a = ap.parse_args(argv)
    if a.cmd == "export-evals" and not a.skill:
        ap.error("export-evals needs --skill")
    {"skills": cmd_skills, "report": cmd_report, "export-evals": cmd_export}[a.cmd](a)


if __name__ == "__main__":
    main()
