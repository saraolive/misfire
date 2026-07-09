#!/usr/bin/env python3
"""misfire — trigger testing for Claude skills.

A skill that never fires is just a well-formatted file.
https://github.com/saraolive/misfire  ·  MIT
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import requests
    import yaml
except ImportError:
    sys.exit("misfire needs: pip install requests pyyaml")

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-4-6"

JUDGE_PROMPT = """You are simulating skill selection for an AI agent.

Below is the catalog of available skills (name + description), followed by a user prompt.
Decide which single skill, if any, the agent should consult to handle the prompt.
Only choose a skill if the prompt genuinely calls for it — for simple prompts the agent
handles directly, choose none.

<catalog>
{catalog}
</catalog>

<user_prompt>
{prompt}
</user_prompt>

Respond with ONLY a JSON object, no other text:
{{"skill": "<skill-name or null>"}}"""


# ---------------------------------------------------------------- catalog

def load_catalog(skills_dir: Path) -> dict[str, str]:
    """Map skill name -> description from every SKILL.md under skills_dir."""
    catalog: dict[str, str] = {}
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        text = skill_md.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        if not m:
            continue
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            continue
        name = meta.get("name") or skill_md.parent.name
        desc = (meta.get("description") or "").strip()
        if desc:
            catalog[str(name)] = desc
    if not catalog:
        sys.exit(f"misfire: no skills with descriptions found in {skills_dir}")
    return catalog


def render_catalog(catalog: dict[str, str]) -> str:
    return "\n\n".join(f"### {n}\n{d}" for n, d in catalog.items())


# ---------------------------------------------------------------- API

def api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        sys.exit("misfire: set ANTHROPIC_API_KEY")
    return key


def judge_once(catalog_str: str, prompt: str, model: str) -> str | None:
    """One trial: which skill (if any) would the agent consult?"""
    resp = requests.post(
        API_URL,
        headers={
            "x-api-key": api_key(),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 100,
            "temperature": 1.0,  # triggering is stochastic; sample it honestly
            "messages": [{
                "role": "user",
                "content": JUDGE_PROMPT.format(catalog=catalog_str, prompt=prompt),
            }],
        },
        timeout=60,
    )
    resp.raise_for_status()
    text = "".join(b.get("text", "") for b in resp.json().get("content", []))
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        skill = json.loads(m.group(0)).get("skill")
    except json.JSONDecodeError:
        return None
    return None if skill in (None, "null", "none", "") else str(skill)


# ---------------------------------------------------------------- runner

@dataclass
class TestResult:
    skill: str
    prompt: str
    kind: str                      # "positive" | "negative"
    trigger_rate: float
    runs: int
    passed: bool
    stolen_by: dict[str, int] = field(default_factory=dict)  # other skills that fired


def run_prompt(catalog: dict[str, str], skill: str, prompt: str, kind: str,
               runs: int, pass_thr: float, fail_thr: float, model: str,
               workers: int = 5) -> TestResult:
    catalog_str = render_catalog(catalog)
    hits, thieves = 0, {}
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(judge_once, catalog_str, prompt, model) for _ in range(runs)]
        for f in futures:
            chosen = f.result()
            if chosen == skill:
                hits += 1
            elif chosen:
                thieves[chosen] = thieves.get(chosen, 0) + 1
    rate = hits / runs
    passed = rate >= pass_thr if kind == "positive" else rate <= fail_thr
    return TestResult(skill, prompt, kind, rate, runs, passed, thieves)


def run_suite(spec_path: Path, runs_override: int | None, as_json: bool) -> int:
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    settings = spec.get("settings", {})
    runs = runs_override or int(settings.get("runs_per_prompt", 10))
    pass_thr = float(settings.get("pass_threshold", 0.8))
    fail_thr = float(settings.get("fail_threshold", 0.2))
    model = settings.get("model", DEFAULT_MODEL)
    skills_dir = (spec_path.parent / spec.get("skills_dir", "./skills")).resolve()
    catalog = load_catalog(skills_dir)

    results: list[TestResult] = []
    for t in spec.get("tests", []):
        skill = t["skill"]
        if skill not in catalog:
            print(f"⚠ skipping unknown skill: {skill} (not in {skills_dir})")
            continue
        for kind in ("positive", "negative"):
            for prompt in t.get(kind, []):
                results.append(run_prompt(catalog, skill, prompt, kind,
                                          runs, pass_thr, fail_thr, model))

    failures = [r for r in results if not r.passed]
    if as_json:
        print(json.dumps({
            "total": len(results), "failures": len(failures),
            "results": [r.__dict__ for r in results],
        }, indent=2))
    else:
        report(results, pass_thr, fail_thr)
    return 1 if failures else 0


def report(results: list[TestResult], pass_thr: float, fail_thr: float) -> None:
    by_skill: dict[str, list[TestResult]] = {}
    for r in results:
        by_skill.setdefault(r.skill, []).append(r)
    for skill, rs in by_skill.items():
        print(f"\n  {skill}")
        for r in rs:
            mark = "✓" if r.passed else "✗"
            tag = ""
            if not r.passed:
                tag = "  ← MISFIRE" if r.kind == "negative" else "  ← UNDERTRIGGERS"
                if r.stolen_by:
                    top = max(r.stolen_by, key=r.stolen_by.get)  # type: ignore[arg-type]
                    tag += f" (lost to: {top} ×{r.stolen_by[top]})"
            print(f"    {mark} {r.kind:<8} {r.prompt[:48]!r:<52} "
                  f"{int(r.trigger_rate * r.runs)}/{r.runs} triggered{tag}")
    fails = sum(1 for r in results if not r.passed)
    print(f"\n  {len(by_skill)} skills · {len(results)} tests · "
          f"{fails} failure{'s' if fails != 1 else ''}\n")


# ---------------------------------------------------------------- corpus mode

def extract_trigger_examples(description: str) -> list[str]:
    """Pull quoted trigger phrases out of a skill's own description."""
    quoted = re.findall(r'[\"\u201c\u2018\']([^\"\u201d\u2019\']{6,90})[\"\u201d\u2019\']', description)
    return [q.strip() for q in quoted if " " in q][:6]


def run_corpus(skills_dir: Path, runs: int, model: str) -> int:
    catalog = load_catalog(skills_dir)
    print(f"\nmisfire corpus · {len(catalog)} skills · testing each against "
          f"its own description's trigger examples\n")
    results: list[TestResult] = []
    for skill, desc in catalog.items():
        examples = extract_trigger_examples(desc)
        if not examples:
            print(f"  ∅ {skill}: no quoted trigger examples in description — "
                  f"nothing to test (that's a finding in itself)")
            continue
        for ex in examples:
            results.append(run_prompt(catalog, skill, ex, "positive",
                                      runs, 0.8, 0.2, model))
    if results:
        report(results, 0.8, 0.2)
    return 1 if any(not r.passed for r in results) else 0


# ---------------------------------------------------------------- suggest mode

def run_suggest(skills_dir: Path, skill: str, model: str) -> int:
    catalog = load_catalog(skills_dir)
    if skill not in catalog:
        sys.exit(f"misfire: skill '{skill}' not found in {skills_dir}")
    prompt = (
        "Given this skill description, write 5 realistic user prompts that SHOULD "
        "trigger it (varied phrasing, not copies of the description) and 3 adjacent "
        "prompts that should NOT trigger it (similar words, different intent). "
        "Respond as YAML with keys positive: and negative:.\n\n"
        f"Skill: {skill}\nDescription:\n{catalog[skill]}"
    )
    resp = requests.post(
        API_URL,
        headers={"x-api-key": api_key(), "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": model, "max_tokens": 800,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=60,
    )
    resp.raise_for_status()
    print("".join(b.get("text", "") for b in resp.json().get("content", [])))
    return 0


# ---------------------------------------------------------------- cli

def main() -> None:
    p = argparse.ArgumentParser(prog="misfire",
                                description="Trigger testing for Claude skills.")
    sub = p.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="run a skill-tests.yaml suite")
    run_p.add_argument("spec", type=Path)
    run_p.add_argument("--runs", type=int, default=None,
                       help="override runs_per_prompt (use 3 for quick local checks)")
    run_p.add_argument("--json", action="store_true")

    cor_p = sub.add_parser("corpus", help="auto-test every skill in a directory")
    cor_p.add_argument("skills_dir", type=Path)
    cor_p.add_argument("--runs", type=int, default=5)
    cor_p.add_argument("--model", default=DEFAULT_MODEL)

    sug_p = sub.add_parser("suggest", help="generate candidate test prompts for a skill")
    sug_p.add_argument("skill")
    sug_p.add_argument("--skills-dir", type=Path, default=Path("./skills"))
    sug_p.add_argument("--model", default=DEFAULT_MODEL)

    a = p.parse_args()
    if a.cmd == "run":
        sys.exit(run_suite(a.spec, a.runs, a.json))
    elif a.cmd == "corpus":
        sys.exit(run_corpus(a.skills_dir, a.runs, a.model))
    elif a.cmd == "suggest":
        sys.exit(run_suggest(a.skills_dir, a.skill, a.model))


if __name__ == "__main__":
    main()
