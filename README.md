# NYC Taxi Trip-Efficiency Pipeline

**FDE Data Foundations — Assignment 2, Track B (NYC TLC)**

From messy, multi-system source extracts to a dependable, repeatable KPI that
tells NYC TLC operations **where and when to intervene in street congestion**.

```
ingest ──▶ completeness ──▶ validate ──▶ model ──▶ metrics
  │             │              │           │          │
 raw files   6 checks      8 rules     star      5 marts
 + manifest  abort on      flag,      schema    partitioned
 (SHA-256)   failure    never delete            by month
```

**20.7M trips · 6 months · 2 fleets · 3 retrieval modes · 190 automated checks**

*Rebuilds all six months from raw in **60s**; ~80s including the first download of 335 MB.*

---

## 1. The problem

TLC operations has a budget for curb management, signal retiming and dispatch
policy, and no defensible way to decide **where to spend it**. Trip data exists,
but it is split across two fleets with incompatible schemas, contains physically
impossible records, and carries no weather context — so the obvious question
*"is this congestion, or is it just raining?"* cannot be answered from it as
delivered.

### Users and stakeholders

| Stakeholder | What they need from this |
|---|---|
| **TLC Operations lead** *(primary)* | A ranked list of zones and hours to target |
| Policy / planning | Whether congestion is trending, and whether rain explains it |
| Data team | A pipeline that reruns safely and fails loudly |

### The KPI

> **Congested trip rate** — the share of valid trips whose average speed is
> below **8 mph**.

Average speed is the only defensible congestion proxy these fields support:
duration alone cannot tell a *long* trip from a *slow* one. The 8 mph line is
calibrated to the data — median trip speed is 9.7 mph — and lives in
`config/pipeline.yaml`, not in code. **Its sensitivity is measured and published;
see §6.**

### The decision it supports

Rank zones and hours for intervention, then measure month over month whether the
intervention moved the number.

---

## 2. Headline findings

| # | Finding |
|---|---|
| **1** | **Manhattan midday is the worst segment in the city** — weekday 11:00–15:00 runs **57.3%** congested vs **44.3%** for the 17:00–19:00 evening peak. Congestion is a *midday* problem, not a rush-hour one. |
| **2** | Congestion **rose 6.4 points** from January (31.5%) to its May peak (37.9%). |
| **3** | Queens looks excellent (2.8%) but that is an **airport artefact** — airport runs are highway trips averaging 23.1 mph. Not free-flowing streets. |
| **4** | **Rain is real but secondary**: +5.0pp and −5.7% network speed. Congestion here is mostly structural. |
| **5** | **The upstream feed broke in March 2024** — missing `passenger_count` doubled from 5.8% to ~12.5%. Caught by the pipeline; the KPI was unaffected *by design* (see §6). |

Finding 1 is the actionable one: it points enforcement at the middle of the day,
which is not where an intuition-led programme would aim first.

Full table: [`docs/evidence_table.md`](docs/evidence_table.md) · Charts:
[`notebooks/05_metrics_dashboard.ipynb`](notebooks/05_metrics_dashboard.ipynb)

---

## 3. Sources

| Source | Mode | Grain | Owner |
|---|---|---|---|
| Yellow taxi trips | **FILE** (Parquet/HTTPS) | 1 row / trip (~3.4M/mo) | NYC TLC |
| Green taxi trips | **FILE** (Parquet/HTTPS) | 1 row / trip (~56k/mo) | NYC TLC |
| Taxi zone lookup | **FILE** (CSV) | 1 row / zone (265) | NYC TLC |
| Hourly NYC weather | **API** (REST/JSON) | 1 row / hour | Open-Meteo (ERA5) |
| *(all of the above)* | **SQL** | — | DuckDB |

**The two fleets do not share a schema.** Yellow uses `tpep_*` timestamps, green
uses `lpep_*`; green adds `ehail_fee`/`trip_type`; yellow's airport-fee column
changes capitalisation between months. Reconciling them is a real piece of source
work, not a formality — it is contained in
[`src/pipeline/schema.py`](src/pipeline/schema.py) and enforced by a check, so a
future schema change fails loudly rather than producing a column of NULLs.

Full reasoning, ownership and gaps: [`docs/source_map.md`](docs/source_map.md)

---

## 4. Setup and run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Full rebuild from nothing: downloads ~335 MB, ~80s (60s once cached)
PYTHONPATH=src python -m pipeline.run --all

# One month
PYTHONPATH=src python -m pipeline.run --month 2024-03

# Skip re-downloading (data already present)
PYTHONPATH=src python -m pipeline.run --all --from-stage completeness

# Regenerate the evidence table from the published marts
PYTHONPATH=src python -m pipeline.report

# Tests
python -m pytest
```

Change the months, the 8 mph threshold or any abort limit in
[`config/pipeline.yaml`](config/pipeline.yaml) — no code change needed.

> `data/` is gitignored: every byte is reproducible from the pipeline, and
> `data/raw/manifest.json` records each input's source URL and SHA-256 so a
> rebuild is verifiable.

---

## 5. Repository layout

```
config/pipeline.yaml        every threshold and judgement call, in one place
src/pipeline/
  ingest.py                 retrieval: files + API, atomic, idempotent, manifest
  completeness.py           C1-C6: is the data whole and correctly labelled?
  schema.py                 the fleet schema contract and reconciliation
  validate.py               V01-V08: flag, never silently drop
  transform.py              star schema + join integrity checks (C7-C8)
  metrics.py                runs sql/metrics.sql, reconciles the KPI (C9-C10)
  report.py                 regenerates docs/evidence_table.md from the marts
  run.py                    orchestration, CLI, failure handling
sql/metrics.sql             the five metric definitions, readable on their own
notebooks/01..05            source reasoning -> retrieval -> validation ->
                            modelling -> dashboard (executed, outputs included)
docs/                       source map, data model, validation rules,
                            known/unknown/assumption/limitation, evidence table
tests/                      30 tests: rule behaviour, idempotency, failure paths
```

---

## 6. FDE judgement calls

Three decisions shaped the output more than any code did.

### A. Flag, never silently drop — and the March break that vindicated it

No validation rule deletes a row. Each writes a boolean flag, and metrics select
the population they are entitled to use (`is_valid_core`, `is_valid_zone`,
`is_valid_fare`). Different metrics therefore have **different denominators**, and
each published metric carries its own `trips` count.

The payoff was not hypothetical. Missing `passenger_count` (rule **V08**) doubled
in March 2024:

| Jan | Feb | **Mar** | Apr | May | Jun |
|---|---|---|---|---|---|
| 5.8% | 7.3% | **12.9%** | 12.6% | 11.8% | 12.5% |

`passenger_count` feeds **no** headline metric, so V08 was made **flag-only**. Had
it been an exclusion rule — the instinctive choice for a 6%-null field — the
pipeline would have begun discarding ~12% of trips in March, and **the KPI would
have shifted mid-series for a reason having nothing to do with traffic.** The
break would have been invisible in the headline number and near-impossible to
diagnose months later.

Because the rule only flags, the break surfaced in `m_data_quality` where it
belongs, and the KPI stayed clean. *Dropping data you don't need is not free —
it silently couples your metric to someone else's reporting habits.*

### B. The KPI's level is arbitrary; its ranking is not

The 8 mph threshold is a judgement call, so its effect was measured rather than
asserted:

| Threshold | 6 mph | 7 mph | **8 mph** | 9 mph | 10 mph |
|---|---|---|---|---|---|
| Congested | 14.7% | 23.8% | **34.3%** | 45.0% | 55.0% |

The level swings enormously. **But the borough ranking is identical at every
threshold tested** — Manhattan > Brooklyn > Bronx > Queens at 6, 8 and 10 mph.

So the honest framing, stated wherever the KPI appears: **use it for ranking and
for trend; do not quote the absolute percentage.** *"34% of trips are congested"
is a fact about an 8 mph line, not about New York.* The decision this pipeline
actually supports — which zones and hours to target — is robust to the one
arbitrary number in it.

### C. Unknown zones are codes, not places

Zones 264/265 mean "Unknown" and "Outside of NYC". Dropping those trips would
understate citywide volume; keeping them as zones would invent a phantom borough
in every report. Rule V06 excludes them from *geographic* metrics only — which is
why `is_valid_zone` exists separately from `is_valid_core`.

---

## 7. Why the pipeline is dependable

| Property | How |
|---|---|
| **Idempotent** | Each month is a partition, removed and rewritten on rerun. A rerun cannot double-count. Proven by a test that reprocesses a month and asserts identical metrics. |
| **Raw data immutable** | Downloads are atomic (`.part` → rename); `data/raw/` is never modified; every input carries a SHA-256. |
| **Fails loudly** | 10 check families (C1–C10) + 8 rule abort limits. Any breach aborts that month and publishes **nothing** for it. |
| **Fails partially** | In `--all`, one bad month doesn't block five good ones; the run exits non-zero. |
| **Self-reconciling** | C9 asserts valid + excluded = total. C10 recomputes the KPI from the published mart and requires agreement. A trip cannot vanish between raw and mart without the run failing. |
| **Auditable** | Every run writes `logs/run_summary_<ts>.json` — per-stage row counts, every check outcome, duration, git commit. |
| **Resumable** | `--from-stage` restarts mid-pipeline without re-downloading. |

Guards against the specific bugs that silently corrupt metrics: **join fan-out**
(C7 asserts the row count is unchanged across three LEFT JOINs), **schema drift**
(C2), **mislabelled extracts** (C4), and **invalid rows defaulting to FALSE**
(`is_congested` is NULL outside the valid population, so it can never be averaged
in as "not congested").

```
$ python -m pytest
30 passed in 8.36s
```

---

## 8. What this cannot tell you

Stated up front because it bounds every number above:

- **Unmet demand is invisible.** Only *completed* trips are recorded — a zone
  nobody will serve looks perfectly healthy.
- **Taxis are a biased sample**, not a census: heavily Manhattan and airport, and
  ride-hail (several times taxi volume) is absent entirely.
- **No route data**, so we can rank *where* to look but never explain *why*
  without fieldwork.
- **Six months cannot separate trend from season** — the Jan→May rise needs 12
  months before it should be called a trend.
- **~2-month publication lag** makes this a planning instrument, not a live
  monitor.

Full register, including the DST edge case and every assumption:
[`docs/known_unknowns.md`](docs/known_unknowns.md)

---

## 9. Documentation

| Document | Contents |
|---|---|
| [`docs/source_map.md`](docs/source_map.md) | Question → information → source; ownership, grain, gaps |
| [`docs/data_model.md`](docs/data_model.md) | Workflow state machine, ER diagram, derived-field choices |
| [`docs/validation_rules.md`](docs/validation_rules.md) | All 8 rules with business rationale |
| [`docs/known_unknowns.md`](docs/known_unknowns.md) | Known / Unknown / Assumption / Limitation |
| [`docs/evidence_table.md`](docs/evidence_table.md) | Final metrics (generated, never hand-typed) |
| [`docs/demo_script.md`](docs/demo_script.md) | Running order for the 3–5 minute walkthrough |
