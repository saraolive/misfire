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
pip install misfire        # or: copy misfire.py — it's one file, stdlib + requests + pyyaml
export ANTHROPIC_API_KEY=sk-ant-...
```

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
misfire corpus ./skills                   # no spec needed: auto-tests every skill
                                          #   against its own description's trigger examples
misfire suggest risk-sweep                # generates candidate positive/negative
                                          #   prompts for a skill, to seed your spec
```

`corpus` mode is how you audit a skill library you didn't write: point it at any skills directory and get a trigger-rate scorecard with zero setup.

## CI

```yaml
# .github/workflows/misfire.yml
- run: pip install misfire
- run: misfire run skill-tests.yaml --json > misfire-report.json
  env:
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

Gate PRs on trigger accuracy the way you gate them on unit tests. Description edits that would silently break triggering now fail loudly in review.

## Cost & honesty notes

- Each test = `runs_per_prompt` small API calls. A 10-skill suite at 10 runs ≈ a few hundred cheap calls. Use `--runs 3` for quick local iteration, full runs in CI.
- misfire measures a **proxy**: the trigger decision given the catalog, not a full agent session. It correlates strongly with real behavior but simple one-step prompts may under-trigger in real sessions regardless of description quality (models skip skills for tasks they can handle directly). Write substantive test prompts.

## License

MIT
