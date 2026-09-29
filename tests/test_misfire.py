"""Unit tests for misfire. No API key needed: every network call is mocked."""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest
import requests

import misfire


# ---------------------------------------------------------------- helpers

def write_skill(root: Path, folder: str, description: str | None,
                name: str | None = None) -> None:
    d = root / folder
    d.mkdir(parents=True)
    fm = []
    if name:
        fm.append(f"name: {name}")
    if description is not None:
        fm.append(f"description: {description!r}")
    body = "---\n" + "\n".join(fm) + "\n---\n\n# " + folder + "\n"
    (d / "SKILL.md").write_text(body, encoding="utf-8")


def fake_response(status: int, text: str = "", headers: dict | None = None,
                  content_text: str | None = None) -> mock.Mock:
    r = mock.Mock(spec=requests.Response)
    r.status_code = status
    r.headers = headers or {}
    r.text = text
    if content_text is not None:
        r.json.return_value = {"content": [{"type": "text", "text": content_text}]}
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}")
    else:
        r.raise_for_status.return_value = None
    return r


@pytest.fixture
def skills(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    write_skill(root, "recipe-scaler",
                'Scale recipes. Use when user says "scale this recipe" or "adjust for N people".')
    write_skill(root, "risk-sweep",
                'Cross-project risk scan. Trigger on "what is at risk", "risk sweep".')
    return root


@pytest.fixture
def api_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(misfire, "_sleep", lambda s: None)


# ---------------------------------------------------------------- catalog

def test_load_catalog_reads_name_and_description(skills: Path):
    cat = misfire.load_catalog(skills)
    assert set(cat) == {"recipe-scaler", "risk-sweep"}
    assert cat["risk-sweep"].startswith("Cross-project")


def test_load_catalog_falls_back_to_folder_name(tmp_path: Path):
    write_skill(tmp_path, "no-name", "Does things.")
    assert misfire.load_catalog(tmp_path) == {"no-name": "Does things."}


def test_load_catalog_uses_frontmatter_name_over_folder(tmp_path: Path):
    write_skill(tmp_path, "folder", "Does things.", name="real-name")
    assert list(misfire.load_catalog(tmp_path)) == ["real-name"]


def test_load_catalog_skips_skills_without_description(tmp_path: Path):
    write_skill(tmp_path, "described", "Yes.")
    write_skill(tmp_path, "undescribed", None)
    (tmp_path / "nofrontmatter").mkdir()
    (tmp_path / "nofrontmatter" / "SKILL.md").write_text("# just a heading\n")
    assert list(misfire.load_catalog(tmp_path)) == ["described"]


def test_load_catalog_exits_when_empty(tmp_path: Path):
    with pytest.raises(SystemExit):
        misfire.load_catalog(tmp_path)


def test_render_catalog_format():
    out = misfire.render_catalog({"a": "desc a", "b": "desc b"})
    assert out == "### a\ndesc a\n\n### b\ndesc b"


# ---------------------------------------------------------------- trigger examples

def test_extract_trigger_examples_pulls_multiword_quotes():
    desc = 'Use when user says "scale this recipe", "adjust for N people", or "x".'
    assert misfire.extract_trigger_examples(desc) == [
        "scale this recipe", "adjust for N people"]


def test_extract_trigger_examples_handles_curly_quotes_and_caps_at_six():
    desc = " ".join(f"“example number {i}”" for i in range(10))
    out = misfire.extract_trigger_examples(desc)
    assert len(out) == 6
    assert out[0] == "example number 0"


def test_extract_trigger_examples_empty_when_none():
    assert misfire.extract_trigger_examples("No quotes here.") == []


# ---------------------------------------------------------------- API / retry

def test_api_key_missing_exits(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        misfire.api_key()


def test_post_messages_returns_text_on_success(api_env):
    with mock.patch("misfire.requests.post",
                    return_value=fake_response(200, content_text="hello")) as post:
        assert misfire.post_messages({"model": "m"}) == "hello"
    assert post.call_count == 1
    assert post.call_args.kwargs["headers"]["x-api-key"] == "sk-ant-test"


def test_post_messages_retries_on_429_then_succeeds(api_env, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(misfire, "_sleep", sleeps.append)
    responses = [fake_response(429, "rate limited", headers={"retry-after": "2"}),
                 fake_response(529, "overloaded"),
                 fake_response(200, content_text="ok")]
    with mock.patch("misfire.requests.post", side_effect=responses) as post:
        assert misfire.post_messages({"model": "m"}) == "ok"
    assert post.call_count == 3
    assert len(sleeps) == 2
    assert sleeps[0] == 2.0  # honoured Retry-After


def test_post_messages_retries_on_connection_error(api_env):
    responses = [requests.ConnectionError("boom"),
                 requests.Timeout("slow"),
                 fake_response(200, content_text="ok")]
    with mock.patch("misfire.requests.post", side_effect=responses):
        assert misfire.post_messages({"model": "m"}) == "ok"


def test_post_messages_gives_up_after_max_retries(api_env):
    with mock.patch("misfire.requests.post",
                    return_value=fake_response(503, "down")) as post:
        with pytest.raises(misfire.APIError, match="HTTP 503"):
            misfire.post_messages({"model": "m"}, max_retries=2)
    assert post.call_count == 3


def test_post_messages_does_not_retry_client_errors(api_env):
    with mock.patch("misfire.requests.post",
                    return_value=fake_response(401, "bad key")) as post:
        with pytest.raises(requests.HTTPError):
            misfire.post_messages({"model": "m"})
    assert post.call_count == 1


def test_retry_delay_backs_off_and_caps():
    assert misfire._retry_delay(0, None) <= misfire.BACKOFF_BASE * 1.5
    assert misfire._retry_delay(20, None) <= misfire.BACKOFF_CAP * 1.5
    r = fake_response(429, headers={"retry-after": "999"})
    assert misfire._retry_delay(0, r) == misfire.BACKOFF_CAP
    r = fake_response(429, headers={"retry-after": "garbage"})
    assert misfire._retry_delay(0, r) > 0


# ---------------------------------------------------------------- judge parsing

@pytest.mark.parametrize("reply, expected", [
    ('{"skill": "risk-sweep"}', "risk-sweep"),
    ('Sure! {"skill": "risk-sweep"} there you go', "risk-sweep"),
    ('{"skill": null}', None),
    ('{"skill": "null"}', None),
    ('{"skill": "none"}', None),
    ('{"skill": ""}', None),
    ("no json at all", None),
    ("{not valid json}", None),
])
def test_judge_once_parses_reply(reply, expected):
    with mock.patch("misfire.post_messages", return_value=reply):
        assert misfire.judge_once("catalog", "prompt", "model") == expected


def test_judge_once_sends_catalog_and_prompt():
    with mock.patch("misfire.post_messages", return_value='{"skill": null}') as pm:
        misfire.judge_once("THE CATALOG", "THE PROMPT", "the-model")
    payload = pm.call_args.args[0]
    assert payload["model"] == "the-model"
    assert payload["temperature"] == 1.0
    content = payload["messages"][0]["content"]
    assert "THE CATALOG" in content and "THE PROMPT" in content


# ---------------------------------------------------------------- run_prompt

def judge_sequence(*choices):
    """Return a judge_once stub that yields the given choices in order."""
    it = iter(choices)
    return lambda *_a, **_k: next(it)


def test_run_prompt_positive_passes_at_threshold():
    cat = {"a": "x", "b": "y"}
    with mock.patch("misfire.judge_once", judge_sequence("a", "a", "a", "a", None)):
        r = misfire.run_prompt(cat, "a", "p", "positive", 5, 0.8, 0.2, "m", workers=1)
    assert r.trigger_rate == 0.8 and r.passed and r.stolen_by == {}


def test_run_prompt_positive_fails_and_records_thief():
    cat = {"a": "x", "b": "y"}
    with mock.patch("misfire.judge_once", judge_sequence("a", "b", "b", None, "b")):
        r = misfire.run_prompt(cat, "a", "p", "positive", 5, 0.8, 0.2, "m", workers=1)
    assert r.trigger_rate == 0.2 and not r.passed
    assert r.stolen_by == {"b": 3}


def test_run_prompt_negative_passes_when_quiet():
    with mock.patch("misfire.judge_once", judge_sequence(None, None, "b", None, None)):
        r = misfire.run_prompt({"a": "x", "b": "y"}, "a", "p", "negative", 5, 0.8, 0.2, "m",
                               workers=1)
    assert r.trigger_rate == 0.0 and r.passed


def test_run_prompt_negative_fails_when_it_fires():
    with mock.patch("misfire.judge_once", judge_sequence("a", "a", None, None, None)):
        r = misfire.run_prompt({"a": "x"}, "a", "p", "negative", 5, 0.8, 0.2, "m", workers=1)
    assert r.trigger_rate == 0.4 and not r.passed


# ---------------------------------------------------------------- baseline / regression

def make_result(kind="positive", rate=1.0, baseline=None, passed=True, **kw):
    return misfire.TestResult("s", "p", kind, rate, 10, passed,
                              baseline_rate=baseline, **kw)


@pytest.mark.parametrize("kind, rate, baseline, regressed", [
    ("positive", 0.9, 0.9, False),
    ("positive", 0.7, 0.9, True),      # dropped exactly 0.2
    ("positive", 0.8, 0.9, False),     # dropped 0.1, within tolerance
    ("positive", 1.0, 0.5, False),     # improved
    ("negative", 0.3, 0.1, True),      # fires more: regression for a negative
    ("negative", 0.0, 0.3, False),     # fires less: improvement
    ("positive", 0.5, None, False),    # no baseline
])
def test_regressed_property(kind, rate, baseline, regressed):
    assert make_result(kind, rate, baseline).regressed is regressed


def test_delta_none_without_baseline():
    assert make_result(baseline=None).delta is None
    assert make_result(rate=0.6, baseline=0.9).delta == pytest.approx(-0.3)


def test_save_and_load_baseline_roundtrip(tmp_path: Path):
    results = [
        misfire.TestResult("a", "p1", "positive", 0.9, 10, True, {"b": 1}),
        misfire.TestResult("a", "n1", "negative", 0.1, 10, True),
    ]
    path = tmp_path / "baseline.json"
    misfire.save_baseline(path, results, "model-x", 10)
    data = json.loads(path.read_text())
    assert data["model"] == "model-x" and data["runs_per_prompt"] == 10
    assert data["total"] == 2 and data["failures"] == 0
    loaded = misfire.load_baseline(path)
    assert loaded == {("a", "positive", "p1"): 0.9, ("a", "negative", "n1"): 0.1}


def test_apply_baseline_matches_on_skill_kind_prompt():
    results = [misfire.TestResult("a", "p1", "positive", 0.5, 10, False),
               misfire.TestResult("a", "p2", "positive", 0.5, 10, False)]
    misfire.apply_baseline(results, {("a", "positive", "p1"): 0.9})
    assert results[0].baseline_rate == 0.9
    assert results[1].baseline_rate is None


# ---------------------------------------------------------------- run_suite end-to-end

def write_spec(tmp_path: Path, skills: Path, runs=5) -> Path:
    spec = tmp_path / "skill-tests.yaml"
    spec.write_text(f"""
settings:
  runs_per_prompt: {runs}
  pass_threshold: 0.8
  fail_threshold: 0.2
  model: test-model
skills_dir: {skills}
tests:
  - skill: recipe-scaler
    positive: ["scale this lasagna for 12"]
    negative: ["how do I scale a web service?"]
  - skill: ghost-skill
    positive: ["never runs"]
""")
    return spec


def constant_judge(mapping: dict[str, str | None]):
    """judge_once stub keyed on prompt text."""
    def _judge(_catalog, prompt, _model):
        for needle, choice in mapping.items():
            if needle in prompt:
                return choice
        return None
    return _judge


def test_run_suite_passes_and_skips_unknown_skill(tmp_path, skills, capsys):
    spec = write_spec(tmp_path, skills)
    judge = constant_judge({"lasagna": "recipe-scaler", "web service": None})
    with mock.patch("misfire.judge_once", judge):
        code = misfire.run_suite(spec, None, as_json=False)
    out = capsys.readouterr()
    assert code == 0
    assert "skipping unknown skill: ghost-skill" in out.err
    assert "5/5 triggered" in out.out and "0/5 triggered" in out.out
    assert "1 skills · 2 tests · 0 failures" in out.out


def test_run_suite_fails_on_misfire_and_reports_thief(tmp_path, skills, capsys):
    spec = write_spec(tmp_path, skills)
    judge = constant_judge({"lasagna": "risk-sweep", "web service": "recipe-scaler"})
    with mock.patch("misfire.judge_once", judge):
        code = misfire.run_suite(spec, None, as_json=False)
    out = capsys.readouterr().out
    assert code == 1
    assert "UNDERTRIGGERS (lost to: risk-sweep ×5)" in out
    assert "MISFIRE" in out
    assert "2 failures" in out


def test_run_suite_json_output_and_overrides(tmp_path, skills, capsys):
    spec = write_spec(tmp_path, skills)
    seen_models = []

    def judge(_c, prompt, model):
        seen_models.append(model)
        return "recipe-scaler" if "lasagna" in prompt else None

    with mock.patch("misfire.judge_once", judge):
        code = misfire.run_suite(spec, 3, as_json=True, model_override="override-model")
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data["model"] == "override-model" and data["runs_per_prompt"] == 3
    assert set(seen_models) == {"override-model"}
    assert data["total"] == 2 and data["failures"] == 0
    assert {r["kind"] for r in data["results"]} == {"positive", "negative"}
    assert all(r["baseline_rate"] is None for r in data["results"])


def test_run_suite_save_then_compare_flags_regression(tmp_path, skills, capsys):
    spec = write_spec(tmp_path, skills, runs=10)
    baseline = tmp_path / "baseline.json"

    # Run 1: healthy, save baseline.
    good = constant_judge({"lasagna": "recipe-scaler", "web service": None})
    with mock.patch("misfire.judge_once", good):
        assert misfire.run_suite(spec, None, False, save=baseline) == 0
    assert "baseline saved to" in capsys.readouterr().out
    assert baseline.exists()

    # Run 2: positive drops from 10/10 to 8/10. Still passes (>= 0.8) but regressed.
    calls = {"n": 0}

    def degraded(_c, prompt, _m):
        if "lasagna" in prompt:
            calls["n"] += 1
            return "recipe-scaler" if calls["n"] <= 8 else None
        return None

    with mock.patch("misfire.judge_once", degraded):
        code = misfire.run_suite(spec, None, False, compare=baseline)
    out = capsys.readouterr().out
    assert code == 0                      # regression alone doesn't fail
    assert "8/10 triggered  was 10/10  Δ -0.20  ↓ REGRESSED" in out
    assert "0/10 triggered  was 0/10  Δ 0.00" in out
    assert "1 regression" in out

    # Same run with --fail-on-regression exits 1.
    calls["n"] = 0
    with mock.patch("misfire.judge_once", degraded):
        code = misfire.run_suite(spec, None, False, compare=baseline,
                                 fail_on_regression=True)
    capsys.readouterr()
    assert code == 1


def test_run_suite_compare_marks_new_prompts(tmp_path, skills, capsys):
    spec = write_spec(tmp_path, skills)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"results": []}))
    good = constant_judge({"lasagna": "recipe-scaler"})
    with mock.patch("misfire.judge_once", good):
        assert misfire.run_suite(spec, None, False, compare=baseline) == 0
    out = capsys.readouterr().out
    assert out.count("(new)") == 2
    assert "0 regressions" in out


def test_run_suite_json_includes_regression_count(tmp_path, skills, capsys):
    spec = write_spec(tmp_path, skills, runs=10)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"results": [
        {"skill": "recipe-scaler", "kind": "positive",
         "prompt": "scale this lasagna for 12", "trigger_rate": 1.0}]}))
    with mock.patch("misfire.judge_once", constant_judge({})):  # nothing fires
        code = misfire.run_suite(spec, None, True, compare=baseline)
    data = json.loads(capsys.readouterr().out)
    assert code == 1
    assert data["regressions"] == 1
    pos = next(r for r in data["results"] if r["kind"] == "positive")
    assert pos["baseline_rate"] == 1.0 and pos["trigger_rate"] == 0.0


# ---------------------------------------------------------------- corpus / suggest

def test_run_corpus_uses_description_examples(skills, capsys):
    with mock.patch("misfire.judge_once", constant_judge({"risk": "risk-sweep"})):
        code = misfire.run_corpus(skills, 3, "m")
    out = capsys.readouterr().out
    assert code == 1  # recipe-scaler examples never fire
    assert "2 skills" in out
    assert "'what is at risk'" in out and "3/3 triggered" in out


def test_run_corpus_reports_skills_without_examples(tmp_path, capsys):
    write_skill(tmp_path, "quiet", "No quoted examples at all.")
    with mock.patch("misfire.judge_once") as j:
        assert misfire.run_corpus(tmp_path, 3, "m") == 0
    assert j.call_count == 0
    assert "∅ quiet: no quoted trigger examples" in capsys.readouterr().out


def test_run_suggest_prints_model_reply(skills, capsys):
    with mock.patch("misfire.post_messages", return_value="positive:\n  - x\n") as pm:
        assert misfire.run_suggest(skills, "risk-sweep", "m") == 0
    assert "positive:" in capsys.readouterr().out
    assert "Skill: risk-sweep" in pm.call_args.args[0]["messages"][0]["content"]


def test_run_suggest_unknown_skill_exits(skills):
    with pytest.raises(SystemExit):
        misfire.run_suggest(skills, "nope", "m")


# ---------------------------------------------------------------- cli

def test_cli_run_wires_flags(tmp_path):
    with mock.patch("misfire.run_suite", return_value=0) as rs:
        with pytest.raises(SystemExit) as e:
            misfire.main(["run", "spec.yaml", "--runs", "3", "--model", "mm", "--json",
                          "--save", "b.json", "--compare", "old.json",
                          "--fail-on-regression"])
    assert e.value.code == 0
    rs.assert_called_once_with(Path("spec.yaml"), 3, True, "mm",
                               Path("b.json"), Path("old.json"), True)


def test_cli_fail_on_regression_requires_compare():
    with pytest.raises(SystemExit) as e:
        misfire.main(["run", "spec.yaml", "--fail-on-regression"])
    assert "requires --compare" in str(e.value)


def test_cli_corpus_and_suggest_dispatch():
    with mock.patch("misfire.run_corpus", return_value=0) as rc:
        with pytest.raises(SystemExit):
            misfire.main(["corpus", "./skills", "--runs", "2"])
    rc.assert_called_once_with(Path("./skills"), 2, misfire.DEFAULT_MODEL)
    with mock.patch("misfire.run_suggest", return_value=0) as rsg:
        with pytest.raises(SystemExit):
            misfire.main(["suggest", "risk-sweep", "--skills-dir", "s"])
    rsg.assert_called_once_with(Path("s"), "risk-sweep", misfire.DEFAULT_MODEL)
