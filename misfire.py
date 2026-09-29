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
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    import requests
    import yaml
except ImportError:
    sys.exit("misfire needs: pip install requests pyyaml")

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-4-6"

# Retry policy for the API. 429 and 5xx are retried with exponential backoff;
# a Retry-After header, when present, wins over the computed delay.
MAX_RETRIES = 5
BACKOFF_BASE = 1.0      # seconds; doubles each attempt
BACKOFF_CAP = 30.0
RETRY_STATUSES = {429, 500, 502, 503, 504, 529}

# A prompt whose trigger rate drops by at least this much versus the baseline
# is flagged as a regression, even if it still clears the pass threshold.
REGRESSION_TOLERANCE = 0.2

_sleep = time.sleep  # swapped out in tests

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


class APIError(RuntimeError):
    """Raised when the API keeps failing after all retries."""


def _retry_delay(attempt: int, resp: requests.Response | None) -> float:
    if resp is not None:
        ra = resp.headers.get("retry-after")
        if ra:
            try:
                return min(float(ra), BACKOFF_CAP)
            except ValueError:
                pass
    delay = min(BACKOFF_BASE * (2 ** attempt), BACKOFF_CAP)
    return delay * (0.5 + random.random())  # jitter: 0.5x .. 1.5x


def post_messages(payload: dict, max_retries: int = MAX_RETRIES) -> str:
    """POST to the Messages API and return the concatenated text of the reply.

    Retries on rate limits (429), overload (529), server errors (5xx), and
    connection/timeout failures. Anything else raises immediately.
    """
    headers = {
        "x-api-key": api_key(),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    last_err: str = ""
    for attempt in range(max_retries + 1):
        resp = None
        try:
            resp = requests.post(API_URL, headers=headers, json=payload, timeout=60)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = f"{type(e).__name__}: {e}"
        else:
            if resp.status_code < 400:
                body = resp.json()
                return "".join(b.get("text", "") for b in body.get("content", []))
            if resp.status_code not in RETRY_STATUSES:
                resp.raise_for_status()
            last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
        if attempt < max_retries:
            _sleep(_retry_delay(attempt, resp))
    raise APIError(f"gave up after {max_retries + 1} attempts — {last_err}")


def judge_once(catalog_str: str, prompt: str, model: str) -> str | None:
    """One trial: which skill (if any) would the agent consult?"""
    text = post_messages({
        "model": model,
        "max_tokens": 100,
        "temperature": 1.0,  # triggering is stochastic; sample it honestly
        "messages": [{
            "role": "user",
            "content": JUDGE_PROMPT.format(catalog=catalog_str, prompt=prompt),
        }],
    })
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
    baseline_rate: float | None = None  # set in --compare mode

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.skill, self.kind, self.prompt)

    @property
    def delta(self) -> float | None:
        if self.baseline_rate is None:
            return None
        return self.trigger_rate - self.baseline_rate

    @property
    def regressed(self) -> bool:
        """Positive prompts regress when they fire less; negatives when they fire more."""
        d = self.delta
        if d is None:
            return False
        return (-d if self.kind == "positive" else d) >= REGRESSION_TOLERANCE - 1e-9


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


# ---------------------------------------------------------------- baselines

def results_to_json(results: list[TestResult], model: str, runs: int) -> dict:
    failures = sum(1 for r in results if not r.passed)
    return {
        "model": model,
        "runs_per_prompt": runs,
        "total": len(results),
        "failures": failures,
        "regressions": sum(1 for r in results if r.regressed),
        "results": [r.__dict__ for r in results],
    }


def save_baseline(path: Path, results: list[TestResult], model: str, runs: int) -> None:
    path.write_text(json.dumps(results_to_json(results, model, runs), indent=2) + "\n",
                    encoding="utf-8")


def load_baseline(path: Path) -> dict[tuple[str, str, str], float]:
    """Map (skill, kind, prompt) -> trigger_rate from a saved report."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        (r["skill"], r["kind"], r["prompt"]): float(r["trigger_rate"])
        for r in data.get("results", [])
    }


def apply_baseline(results: list[TestResult],
                   baseline: dict[tuple[str, str, str], float]) -> None:
    for r in results:
        r.baseline_rate = baseline.get(r.key)


# ---------------------------------------------------------------- suite

def run_suite(spec_path: Path, runs_override: int | None, as_json: bool,
              model_override: str | None = None,
              save: Path | None = None, compare: Path | None = None,
              fail_on_regression: bool = False) -> int:
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    settings = spec.get("settings", {})
    runs = runs_override or int(settings.get("runs_per_prompt", 10))
    pass_thr = float(settings.get("pass_threshold", 0.8))
    fail_thr = float(settings.get("fail_threshold", 0.2))
    model = model_override or settings.get("model", DEFAULT_MODEL)
    skills_dir = (spec_path.parent / spec.get("skills_dir", "./skills")).resolve()
    catalog = load_catalog(skills_dir)

    baseline = load_baseline(compare) if compare else None

    results: list[TestResult] = []
    for t in spec.get("tests", []):
        skill = t["skill"]
        if skill not in catalog:
            print(f"⚠ skipping unknown skill: {skill} (not in {skills_dir})", file=sys.stderr)
            continue
        for kind in ("positive", "negative"):
            for prompt in t.get(kind, []):
                results.append(run_prompt(catalog, skill, prompt, kind,
                                          runs, pass_thr, fail_thr, model))

    if baseline is not None:
        apply_baseline(results, baseline)
    if save:
        save_baseline(save, results, model, runs)

    failures = [r for r in results if not r.passed]
    regressions = [r for r in results if r.regressed]
    if as_json:
        print(json.dumps(results_to_json(results, model, runs), indent=2))
    else:
        report(results, pass_thr, fail_thr, compared=baseline is not None)
        if save:
            print(f"  baseline saved to {save}\n")

    if failures:
        return 1
    if fail_on_regression and regressions:
        return 1
    return 0


def _fmt_hits(rate: float, runs: int) -> str:
    return f"{int(round(rate * runs))}/{runs}"


def report(results: list[TestResult], pass_thr: float, fail_thr: float,
           compared: bool = False) -> None:
    by_skill: dict[str, list[TestResult]] = {}
    for r in results:
        by_skill.setdefault(r.skill, []).append(r)
    for skill, rs in by_skill.items():
        print(f"\n  {skill}")
        for r in rs:
            mark = "✓" if r.passed else "✗"
            line = (f"    {mark} {r.kind:<8} {r.prompt[:48]!r:<52} "
                    f"{_fmt_hits(r.trigger_rate, r.runs)} triggered")
            if compared:
                if r.baseline_rate is None:
                    line += "  (new)"
                else:
                    d = r.delta or 0.0
                    sign = "+" if d > 0 else ""
                    line += f"  was {_fmt_hits(r.baseline_rate, r.runs)}  Δ {sign}{d:.2f}"
            tag = ""
            if not r.passed:
                tag = "  ← MISFIRE" if r.kind == "negative" else "  ← UNDERTRIGGERS"
                if r.stolen_by:
                    top = max(r.stolen_by, key=r.stolen_by.get)  # type: ignore[arg-type]
                    tag += f" (lost to: {top} ×{r.stolen_by[top]})"
            elif r.regressed:
                tag = "  ↓ REGRESSED"
            print(line + tag)
    fails = sum(1 for r in results if not r.passed)
    summary = (f"\n  {len(by_skill)} skills · {len(results)} tests · "
               f"{fails} failure{'s' if fails != 1 else ''}")
    if compared:
        regs = sum(1 for r in results if r.regressed)
        summary += f" · {regs} regression{'s' if regs != 1 else ''}"
    print(summary + "\n")


# ---------------------------------------------------------------- corpus mode

def extract_trigger_examples(description: str) -> list[str]:
    """Pull quoted trigger phrases out of a skill's own description."""
    quoted = re.findall(r'[\"“‘\']([^\"”’\']{6,90})[\"”’\']', description)
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
    print(post_messages({
        "model": model, "max_tokens": 800,
        "messages": [{"role": "user", "content": prompt}],
    }))
    return 0


# ---------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="misfire",
                                description="Trigger testing for Claude skills.")
    sub = p.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="run a skill-tests.yaml suite")
    run_p.add_argument("spec", type=Path)
    run_p.add_argument("--runs", type=int, default=None,
                       help="override runs_per_prompt (use 3 for quick local checks)")
    run_p.add_argument("--model", default=None, help="override settings.model")
    run_p.add_argument("--json", action="store_true", help="machine-readable report")
    run_p.add_argument("--save", type=Path, metavar="FILE",
                       help="write this run's results as a baseline JSON file")
    run_p.add_argument("--compare", type=Path, metavar="FILE",
                       help="show trigger-rate deltas against a saved baseline")
    run_p.add_argument("--fail-on-regression", action="store_true",
                       help="with --compare: exit 1 if any prompt's rate moved the wrong "
                            "way by 20%% or more, even if it still passes its threshold")

    cor_p = sub.add_parser("corpus", help="auto-test every skill in a directory")
    cor_p.add_argument("skills_dir", type=Path)
    cor_p.add_argument("--runs", type=int, default=5)
    cor_p.add_argument("--model", default=DEFAULT_MODEL)

    sug_p = sub.add_parser("suggest", help="generate candidate test prompts for a skill")
    sug_p.add_argument("skill")
    sug_p.add_argument("--skills-dir", type=Path, default=Path("./skills"))
    sug_p.add_argument("--model", default=DEFAULT_MODEL)
    return p


def main(argv: list[str] | None = None) -> None:
    a = build_parser().parse_args(argv)
    if a.cmd == "run":
        if a.fail_on_regression and not a.compare:
            sys.exit("misfire: --fail-on-regression requires --compare")
        sys.exit(run_suite(a.spec, a.runs, a.json, a.model,
                           a.save, a.compare, a.fail_on_regression))
    elif a.cmd == "corpus":
        sys.exit(run_corpus(a.skills_dir, a.runs, a.model))
    elif a.cmd == "suggest":
        sys.exit(run_suggest(a.skills_dir, a.skill, a.model))


if __name__ == "__main__":
    main()
