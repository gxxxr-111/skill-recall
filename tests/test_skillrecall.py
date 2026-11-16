"""Offline tests on synthetic transcripts. Run: python3 -m unittest discover tests"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/skill-recall/scripts"))
import skillrecall as sr  # noqa: E402

LISTING = ("- pdf-tools: Extract text and tables from PDF files, merge or split PDFs.\n"
           "- plugin:deploy: Deploy the web app to staging or production with rollback.\n"
           "- quiet-skill: Translate subtitles between languages.")


def entry(kind, ts, **kw):
    d = {"type": kind, "timestamp": ts, "sessionId": kw.pop("sid", "s1"), "cwd": kw.pop("cwd", "/proj")}
    d.update(kw)
    return json.dumps(d, ensure_ascii=False)


def user(text, ts, **kw):
    return entry("user", ts, message={"role": "user", "content": text}, promptSource="typed", **kw)


def skill_call(name, ts):
    return entry("assistant", ts, message={"role": "assistant", "content": [
        {"type": "tool_use", "id": "t", "name": "Skill", "input": {"skill": name}}]})


def listing(ts, content=LISTING, names=("pdf-tools", "plugin:deploy", "quiet-skill")):
    return entry("attachment", ts, attachment={"type": "skill_listing", "content": content,
                                               "names": list(names), "isInitial": True})


class Parsing(unittest.TestCase):
    def test_listing_with_colon_names_and_dropped_descriptions(self):
        listed, dropped = sr.parse_listing({"content": LISTING, "names": ["pdf-tools", "plugin:deploy", "quiet-skill", "bare"]})
        self.assertEqual(listed["plugin:deploy"][:6], "Deploy")
        self.assertEqual(dropped, {"bare"})

    def test_prompts_commands_and_noise(self):
        s = sr.parse_transcript([
            listing("2026-11-01T00:00:00Z"),
            user("please extract the tables from report.pdf", "2026-11-01T00:00:01Z"),
            user("<command-name>/deploy</command-name>", "2026-11-01T00:00:02Z"),
            entry("user", "2026-11-01T00:00:03Z", message={"content": "This session is being continued"}, isCompactSummary=True),
            entry("user", "2026-11-01T00:00:04Z", message={"content": [{"type": "tool_result"}]}, toolUseResult={"x": 1}),
            entry("user", "2026-11-01T00:00:05Z", message={"content": "a notification"}, promptSource="system"),
            user("<system-reminder>ignore me</system-reminder>merge these PDFs", "2026-11-01T00:00:06Z"),
            entry("user", "2026-11-01T00:00:07Z", message={"content": "sub"}, isSidechain=True),
        ])
        kinds = [(e.kind, e.text[:20]) for e in s.events]
        self.assertEqual(kinds, [("listing", '["pdf-tools", "plugi'), ("prompt", "please extract the t"),
                                 ("skill", "deploy"), ("prompt", "merge these PDFs")])

    def test_tokens_handle_chinese(self):
        self.assertIn("字幕", sr.tokens("帮我翻译字幕"))
        self.assertIn("pdf", sr.tokens("Merge the PDF files"))


class Analysis(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self.tmp.name) / "projects"
        sr.CACHE = Path(self.tmp.name) / "cache"
        sr.JUDGE_CWD = sr.CACHE / "judge-workdir"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, lines, folder="p1"):
        d = self.proj / folder
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.jsonl").write_text("\n".join(lines) + "\n")

    def run_all(self, threshold=1.0):
        sessions, stats = sr.load_sessions(self.proj, None, None)
        skills = sr.merge_inventory([], sessions)
        pairs, uses, used_prompts, dropped = sr.analyze(sessions, skills, threshold, min_norm=0.1)
        return sessions, stats, skills, pairs, uses, used_prompts

    def test_miss_hit_and_timeline(self):
        self.write("a", [
            user("extract the tables from this pdf file", "2026-11-01T00:00:01Z"),   # before listing -> sees first listing
            listing("2026-11-01T00:00:02Z"),
            user("deploy the web app to staging please", "2026-11-01T00:00:03Z"),
            skill_call("plugin:deploy", "2026-11-01T00:00:04Z"),
        ])
        _, _, skills, pairs, uses, used_prompts = self.run_all()
        by = {(p.skill, p.prompt[:7]): p for p in pairs}
        self.assertFalse(by[("pdf-tools", "extract")].used, "pdf request without the skill is a miss")
        self.assertTrue(by[("plugin:deploy", "deploy ")].used)
        self.assertEqual(uses["plugin:deploy"], 1)

    def test_skill_installed_mid_session_is_not_a_miss_before(self):
        late = "- late-skill: Summarize bilibili videos into transcripts."
        self.write("b", [
            listing("2026-11-01T00:00:00Z", content="- pdf-tools: PDF files.", names=["pdf-tools"]),
            user("summarize bilibili videos into transcripts", "2026-11-01T00:00:01Z"),
            listing("2026-11-01T00:00:02Z", content=late, names=["pdf-tools", "late-skill"]),
            user("summarize these bilibili videos into transcripts too", "2026-11-01T00:00:03Z"),
        ])
        *_, pairs, _, _ = self.run_all()
        prompts = [p.prompt for p in pairs if p.skill == "late-skill"]
        self.assertEqual(prompts, ["summarize these bilibili videos into transcripts too"])

    def test_eval_and_scripted_sessions_are_dropped(self):
        for i in range(3):
            self.write(f"eval{i}", [listing("2026-11-01T00:00:00Z"), user("merge my pdf files", "2026-11-01T00:00:01Z", sid=f"e{i}")])
        self.write("script", [user("You are optimizing a skill description for pdf-tools", "2026-11-01T00:00:01Z", sid="x")])
        self.write("real", [listing("2026-11-01T00:00:00Z"), user("merge my pdf files", "2026-11-02T00:00:01Z", sid="r"),
                            user("thanks, now split them", "2026-11-02T00:00:02Z", sid="r")])
        _, stats, *_ = self.run_all()
        self.assertEqual((stats["eval_like"], stats["programmatic"], stats["sessions"]), (3, 1, 1))

    def test_repeated_prompt_counted_once(self):
        for i in range(2):
            self.write(f"r{i}", [listing("2026-11-01T00:00:00Z"), user("extract tables from the pdf", f"2026-11-0{i+1}T00:00:01Z", sid=f"r{i}"),
                                 user("ok", f"2026-11-0{i+1}T00:00:02Z", sid=f"r{i}")])
        *_, pairs, _, _ = self.run_all()
        self.assertEqual(len([p for p in pairs if p.skill == "pdf-tools"]), 1)

    def test_judge_batches_caches_and_summary_is_honest(self):
        self.write("j", [listing("2026-11-01T00:00:00Z"),
                         user("extract tables from the pdf", "2026-11-01T00:00:01Z"),
                         user("merge and split pdf files", "2026-11-01T00:00:02Z"),
                         user("deploy to production with rollback", "2026-11-01T00:00:03Z")])
        sessions, _, skills, pairs, uses, up = self.run_all()
        rows = {r["skill"]: r for r in sr.summarize(skills, pairs, uses, up)}
        self.assertIsNone(rows["pdf-tools"]["est_recall"], "no recall claim before judging")
        calls = []

        def fake(items, model):
            calls.append(len(items))
            return ["pdf" in it["skill"] for it in items]

        n = sr.judge_pairs(pairs, {s.name: s for s in skills}, "haiku", limit=10, batch=2, runner=fake)
        self.assertEqual(n, len(calls))
        self.assertTrue(all(c <= 2 for c in calls))
        rows = {r["skill"]: r for r in sr.summarize(skills, pairs, uses, up)}
        self.assertEqual(rows["pdf-tools"]["confirmed"], 2)
        self.assertEqual(rows["pdf-tools"]["est_recall"], 0.0)
        # cached verdicts are reused: no new calls
        for p in pairs:
            p.verdict = None
        self.assertEqual(sr.judge_pairs(pairs, {s.name: s for s in skills}, "haiku", 10, 2, runner=fake), 0)
        evals = sr.export_evals(pairs, "pdf-tools", 5)
        self.assertTrue(evals and all(e["should_trigger"] for e in evals))

    def test_collisions(self):
        pairs = [sr.Pair(s, f"p{i}", 9, [], "s", False) for i in range(4) for s in ("a", "b")]
        self.assertEqual(sr.collisions(pairs), [("a", "b", 4, 1.0)])

    def test_frontmatter_block_scalar(self):
        fm = sr.parse_frontmatter("---\nname: x\ndescription: >\n  first line\n  second line\nother: y\n---\nbody")
        self.assertEqual(fm, {"name": "x", "description": "first line second line", "other": "y"})


if __name__ == "__main__":
    unittest.main()
