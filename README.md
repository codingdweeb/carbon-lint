# carbon-lint

A linter that flags high-emission code patterns — like ESLint, but for CO₂.

Carbon impact is invisible in the normal dev workflow. A PR that introduces a polling loop, loads an entire dataset to use 3 columns, or defaults to GPT-4 for a yes/no classification looks identical to a clean one in code review. Carbon Lint surfaces those patterns as inline warnings — with estimated CO₂ costs attached — so you can weigh the tradeoff in the same place you're already making decisions.

Powered by [AI Impact Calculator](https://aiimpactcalculator.com).

---

## Install

```bash
pip install carbon-lint
```

Or install from source:

```bash
git clone https://github.com/Gjeesus/carbon-lint
cd carbon-lint
pip install -e .
```

---

## Usage

```bash
# Scan a directory
carbon-lint src/

# Scan specific files
carbon-lint app.py jobs/batch_runner.py

# JSON output (for custom tooling)
carbon-lint src/ --format json

# GitHub Actions annotations (posts inline warnings on PRs)
carbon-lint src/ --format github

# Run only specific rules
carbon-lint src/ --rules MODEL-001 POLL-001
```

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | No warnings |
| `1` | At least one warning |

Add `--exit-zero` to always exit 0 (for reporting-only CI steps).

---

## Rules

| ID | Pattern | CO₂ Impact | Severity |
|----|---------|-----------|---------|
| `POLL-001` | Fixed-interval polling loop (`while True` + `time.sleep(N)`) | ~200–800 g CO₂/hr vs. event-driven | warning |
| `THREAD-001` | `ThreadPoolExecutor()` without `max_workers` | Scales to all cores — cap to bound compute | warning |
| `DATA-001` | `pd.read_csv()` without `usecols` | 2–10× more I/O than a column-filtered read | info |
| `MODEL-001` | Large LLM (GPT-4, Opus, Gemini Pro…) for a potentially simple task | 10–50× more energy per token than smaller models | warning |
| `MODEL-002` | LLM call without `stream=True` | Server buffers full response before delivery | info |
| `CACHE-001` | `requests.get()` inside a loop | N redundant network round-trips | warning |

### JS/TS rules

| ID | Pattern | CO₂ Impact | Severity |
|----|---------|-----------|---------|
| `POLL-001` | `setInterval` with interval < 5000ms | ~100–500 g CO₂/hr vs. push-based approach | warning |
| `FETCH-001` | `fetch()` inside a loop | N redundant network requests | warning |
| `PROMISE-001` | Unbounded `Promise.all()` | Concurrent requests spike server-side compute | info |
| `CACHE-001` | `fetch()` without cache headers | Response re-fetched on every call | info |
| `MODEL-001` | Large LLM model name in API call | 10–50× more energy per token than smaller models | warning |
| `MODEL-002` | LLM `.create()` / `.generate()` without `stream: true` | Server buffers full response before delivery | info |

---

## CI Integration

Copy `.github/workflows/carbon-lint.yml` to your repo. It runs on every PR, scans changed files only, and posts inline annotations on the diff.

To make warnings **block merges**, remove `--exit-zero` from the workflow steps.

---

## Pre-commit Hook

Add to your `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/Gjeesus/carbon-lint
    rev: v0.1.0
    hooks:
      - id: carbon-lint-python
      - id: carbon-lint-js
```

---

## How the CO₂ estimates work

The figures are order-of-magnitude estimates, not exact measurements. The goal is to give enough signal to make a tradeoff, not to bill your carbon account to the milligram.

- **Polling loops**: Idle polling at 1-second intervals on a cloud VM uses ~0.2–0.8 kWh/hour of wasted compute vs. a webhook. At ~450 g CO₂/kWh average grid intensity, that's 90–360 g CO₂/hour saved per instance.
- **Model sizes**: GPT-4 class inference runs at ~10–50× the FLOPs of a 7B-parameter model for equivalent output length (Epoch AI, 2024).
- **Data loading**: Loading a 1 GB CSV vs. a 100 MB column-filtered subset changes memory bandwidth, GC pressure, and can trigger swap — the 2–10× figure is conservative.

---

## Contributing

Each new rule needs three things:

1. A reproducible code pattern (AST node or line-level regex)
2. A CO₂ estimate with a citation or derivation
3. A concrete fix that requires no new dependencies

Open a PR with those three things and a test case.

---

## License

MIT — see [LICENSE](LICENSE).
