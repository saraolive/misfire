# misfire

**Trigger testing for Claude skills. Because a skill that never fires is just a well-formatted file.**

Linters check whether your SKILL.md is *valid*. misfire checks whether it *works* — does your skill actually trigger on the prompts it should, and stay quiet on the ones it shouldn't?

```
$ misfire run skill-tests.yaml

  risk-sweep
    ✓ positive  "what's at risk this week?"          10/10 triggered
    ✓ positive  "any risks I should know about"       9/10 triggered
    ✗ negative  "what's the weather risk for my BBQ"  3/10 triggered  ← MISFIRE

  standup-prep
    ✗ positive  "what should I cover today"           4/10 triggered  ← UNDERTRIGGERS

  2 skills · 4 tests · 2 failures        exit code 1
```

## Why this exists

Skill triggering is **stochastic**. The model reads your skill's name + description and decides — probabilistically — whether to consult it. That means:

- You can't test it with a single run. misfire runs every prompt N times and reports a **trigger rate**.
- You can't assert output equality. The question isn't "did it produce X" — it's "did the skill fire at all."
- Your description is a prompt, and prompts regress. Edit one word, and a skill that fired 9/10 times now fires 4/10. Without tests, you'll never know.

Linters (skill-lint, claudelint, agnix) solve structure. misfire solves behavior. Use both.

## Install

```bash
pipx install git+https://github.com/saraolive/misfire     # or: pip install git+https://...
export ANTHROPIC_API_KEY=sk-ant-...
```

Either line gives you a `misfire` command. Pin a release with `...misfire@v0.2.0`.

Or skip installing entirely: `misfire.py` is one file, stdlib + `requests` + `pyyaml`. Copy it into your repo and run `python misfire.py`.

## Test spec

```yaml
# skill-tests.yaml
settings:
  runs_per_prompt: 10        # trials per prompt (triggering is stochastic)
  pass_threshold: 0.8        # positive prompts must trigger ≥ this rate
  fail_threshold: 0.2        # negative prompts must trigger ≤ this rate
  model: claude-sonnet-4-6

skills_dir: ./skills         # every subfolder with a SKILL.md becomes a candidate

tests:
  - skill: risk-sweep
    positive:                # MUST trigger
      - "what's at risk this week?"
      - "run a risk sweep"
      - "anything I should be worried about across projects?"
    negative:                # must NOT trigger
      - "what's the weather risk for my BBQ on Saturday"
      - "explain risk management as a discipline"

  - skill: standup-prep
    positive:
      - "prep me for standup"
      - "who do I need to check in with this morning?"
    negative:
      - "what is a standup meeting?"
```

## How it works

For each prompt, misfire presents the model with the full catalog of skill names + descriptions from `skills_dir` (exactly the information Claude has at trigger time) and asks which skill, if any, it would consult. N trials → trigger rate → compared against your thresholds.

Testing against the *whole catalog* matters: skills don't misfire in isolation, they misfire by losing to a neighbor with a pushier description. misfire tells you which skill stole the trigger.

## Commands

```bash
misfire run skill-tests.yaml              # run the suite, exit 1 on failure
misfire run skill-tests.yaml --json       # machine-readable report
misfire run skill-tests.yaml --runs 3     # quick local check; --model to override the judge
misfire run skill-tests.yaml --save baseline.json       # record trigger rates
misfire run skill-tests.yaml --compare baseline.json    # show deltas vs. that record
misfire corpus ./skills                   # no spec needed: auto-tests every skill
                                          #   against its own description's trigger examples
misfire overlap ./skills                  # which skills steal each other's prompts
misfire suggest risk-sweep                # generates candidate positive/negative
                                          #   prompts for a skill, to seed your spec
```

`corpus` mode is how you audit a skill library you didn't write: point it at any skills directory and get a trigger-rate scorecard with zero setup.

Skill directories are searched **recursively**, so a plugin repo with `plugins/*/skills/*/SKILL.md` works as-is. `corpus`, `overlap` and `suggest` with no directory argument look in `./.claude/skills`, `./skills` and `~/.claude/skills`, and combine every one that exists. That's the catalog Claude actually sees, so it's the one worth testing.

## Overlap report

A skill rarely misfires alone. It misfires because a neighbour's description is pushier, or because two skills describe the same territory. `overlap` runs every skill's trigger examples against the whole catalog and shows who wins.

```
$ misfire overlap ~/.claude/skills --runs 5

misfire overlap · 6 skills · 23 prompts · 5 runs each

  skill                 own   lost to
  standup-prep         9/20    risk-sweep 8  (none) 3
  stakeholder-update  14/20    sync-all 4  (none) 2
  risk-sweep          18/20    (none) 2
  project-sync        15/15    —

  ∅ no quoted trigger examples, not tested: memory-create

  contested (a neighbour took ≥ 20% of a skill's trials):
    standup-prep ← risk-sweep  8/20
      'what should I be worried about today'   ×5/5
      'who do I need to check in with'         ×3/5
    stakeholder-update ← sync-all  4/20
      'pull the latest before I write the update'   ×4/5
```

- Prompts come from the quoted phrases in each skill's description. Add `--spec skill-tests.yaml` to include your positive prompts too.
- A **contested pair** is a neighbour winning at least 20% of a skill's trials, adjustable with `--threshold`. Any contested pair exits 1, so this can gate a plugin repo the same way `run` gates a spec.
- `(none)` counts trials where no skill fired at all. A high `(none)` with no thief means the description is too weak, not that a neighbour is too strong. It can also mean the quoted phrases in the description aren't user prompts at all (command names, file types), in which case use `--spec` to supply real ones.
- `--json` emits the rows, the contested pairs with their offending prompts, and the list of untested skills.

## Baseline & diff

Thresholds catch a skill that *broke*. They don't catch one that *slipped*: a positive prompt going from 10/10 to 8/10 still passes at 0.8, and the next description edit takes it to 6/10. `--save` and `--compare` make the slip visible.

```
$ misfire run skill-tests.yaml --save baseline.json      # on main, once
$ # ...edit a description...
$ misfire run skill-tests.yaml --compare baseline.json

  risk-sweep
    ✓ positive  "what's at risk this week?"          8/10 triggered  was 10/10  Δ -0.20  ↓ REGRESSED
    ✓ positive  "run a risk sweep"                  10/10 triggered  was 10/10  Δ 0.00
    ✓ negative  "explain risk management"            0/10 triggered  was 1/10   Δ -0.10
    ✓ positive  "what's blocking us right now?"      9/10 triggered  (new)

  1 skills · 4 tests · 0 failures · 1 regression
```

- Results are matched on skill + prompt + kind, so reordering or adding tests is fine; new prompts show as `(new)`.
- A **regression** is a move of 0.2 or more in the wrong direction: positives firing less, negatives firing more. It's reported but doesn't fail the run unless you pass `--fail-on-regression`.
- The baseline file is the same JSON as `--json`, so you can `--save` and `--json` from one run, or commit the baseline next to your spec.
- Keep `runs_per_prompt` the same between baseline and compare, otherwise the deltas are noise.

## CI

```yaml
# .github/workflows/misfire.yml
- run: pip install git+https://github.com/saraolive/misfire
- run: misfire run skill-tests.yaml --compare misfire-baseline.json --fail-on-regression
  env:
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

Gate PRs on trigger accuracy the way you gate them on unit tests. Description edits that would silently break triggering now fail loudly in review.

Commit `misfire-baseline.json` alongside your spec and refresh it on `main` with `--save` whenever you intentionally change a description. Note that forked PRs can't read repository secrets, so on public repos this check only runs for branches in the main repo.

## Cost & honesty notes

- Each test = `runs_per_prompt` small API calls. A 10-skill suite at 10 runs ≈ a few hundred cheap calls. Use `--runs 3` for quick local iteration, full runs in CI.
- Rate limits and transient errors are retried with exponential backoff (up to 5 retries, honouring `Retry-After`). Persistent failures abort the run rather than silently counting as "didn't trigger".
- misfire measures a **proxy**: the trigger decision given the catalog, not a full agent session. It correlates strongly with real behavior but simple one-step prompts may under-trigger in real sessions regardless of description quality (models skip skills for tasks they can handle directly). Write substantive test prompts.

## Development

```bash
git clone https://github.com/saraolive/misfire && cd misfire
pip install -e ".[dev]"
pytest                      # no API key needed; every network call is mocked
```

Bug reports and PRs welcome at [github.com/saraolive/misfire/issues](https://github.com/saraolive/misfire/issues).

## License

MIT
