# NYC Taxi Trip-Efficiency Pipeline

**FDE Data Foundations, Assignment 2, Track B (NYC TLC)**

This project turns raw, multi-source NYC taxi extracts into a dependable,
repeatable KPI that tells NYC TLC operations **where and when to intervene in
street congestion**.

```
ingest ──▶ completeness ──▶ validate ──▶ model ──▶ metrics
  │             │              │           │          │
 raw files   6 checks      8 rules     star      5 marts
 + manifest  abort on      flag,      schema    partitioned
 (SHA-256)   failure    never delete            by month
```

**20.7M trips · 6 months · 2 fleets · 3 retrieval modes · 190 automated checks**

*All six months rebuild from raw in **60s**, or about 80s including the first
download of 335 MB.*

---

## 1. The problem

TLC operations has a budget for curb management, signal retiming and dispatch
policy, but no defensible way to decide **where to spend it**. The trip data
exists, yet it is split across two fleets with incompatible schemas, contains
physically impossible records, and carries no weather context. As delivered, it
cannot answer the obvious question: *is this congestion, or is it just raining?*

### Users and stakeholders

| Stakeholder | What they need from this |
|---|---|
| **TLC Operations lead** *(primary)* | A ranked list of zones and hours to target |
| Policy / planning | Whether congestion is trending, and whether rain explains it |
| Data team | A pipeline that reruns safely and fails loudly |

### The KPI

> **Congested trip rate:** the share of valid trips whose average speed is below
> **8 mph**.

Average speed is the only defensible congestion proxy these fields support,
because duration alone cannot tell a *long* trip from a *slow* one. The 8 mph
threshold is calibrated to the data (the median trip speed is 9.7 mph) and is
set in `config/pipeline.yaml` rather than in code. Its sensitivity is measured
and published in §6.

### The decision it supports

The KPI ranks zones and hours for intervention, and then shows month over month
whether an intervention moved the number.

---

## 2. Headline findings

| # | Finding |
|---|---|
| **1** | **Manhattan at midday is the most congested segment in the city.** Weekdays from 11:00 to 15:00 run **57.3%** congested, compared with **44.3%** for the 17:00–19:00 evening peak. Congestion is a *midday* problem rather than a rush-hour one. |
| **2** | Congestion **rose 6.4 points** from January (31.5%) to a peak in May (37.9%). |
| **3** | Queens appears to have almost no congestion (2.8%), but this is an **airport effect**. Airport runs are mostly highway trips averaging 23.1 mph, so the figure does not reflect free-flowing streets. |
| **4** | **Rain has a real but secondary effect**, adding 5.0 points of congestion and reducing network speed by 5.7%. Most congestion is structural. |
| **5** | **The upstream feed changed in March 2024**, and missing `passenger_count` values doubled from 5.8% to about 12.5%. The pipeline detected the change, and the KPI was unaffected by design (see §6). |

Finding 1 is the most actionable: it directs enforcement to the middle of the
day, which is not where an intuition-led programme would focus first.

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
| All of the above | **SQL** | n/a | DuckDB |

**The two fleets do not share a schema.** Yellow uses `tpep_*` timestamps and
green uses `lpep_*`. Green adds `ehail_fee` and `trip_type`, and the
capitalisation of yellow's airport-fee column changes between months.
Reconciling the two is handled in [`src/pipeline/schema.py`](src/pipeline/schema.py)
and enforced by a check, so a future schema change fails the run instead of
silently producing a column of NULLs.

Ownership, grain, gaps and the reasoning behind each source are in
[`docs/source_map.md`](docs/source_map.md).

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

The months, the 8 mph threshold and every abort limit can be changed in
[`config/pipeline.yaml`](config/pipeline.yaml) without touching code.

> `data/` is gitignored because every byte of it is reproducible from the
> pipeline. `data/raw/manifest.json` records each input's source URL and
> SHA-256, so any rebuild can be verified.

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

## 6. Key design decisions

Three decisions shaped the output more than any individual piece of code.

### A. Flag, never silently drop

No validation rule deletes a row. Each rule writes a boolean flag, and each
metric selects the population it is entitled to use (`is_valid_core`,
`is_valid_zone`, `is_valid_fare`). Different metrics therefore have **different
denominators**, and every published metric carries its own `trips` count.

This paid off in practice. Missing `passenger_count` values (rule **V08**)
doubled in March 2024:

| Jan | Feb | **Mar** | Apr | May | Jun |
|---|---|---|---|---|---|
| 5.8% | 7.3% | **12.9%** | 12.6% | 11.8% | 12.5% |

Because `passenger_count` feeds **no** headline metric, V08 was made
**flag-only**. Excluding those rows would have been the instinctive choice for a
field that was 6% null, but it would have caused the pipeline to discard about
12% of trips from March onward. **The KPI would then have shifted mid-series for
a reason unrelated to traffic.** That break would have been invisible in the
headline number and very hard to diagnose months later.

Since the rule only flags, the change surfaced in `m_data_quality` where it
belongs, and the KPI was unaffected. Dropping data that a metric does not need is
not free, because it quietly ties the metric to someone else's reporting habits.

### B. The KPI's level is arbitrary, but its ranking is not

The 8 mph threshold is a judgement call, so its effect was measured rather than
assumed:

| Threshold | 6 mph | 7 mph | **8 mph** | 9 mph | 10 mph |
|---|---|---|---|---|---|
| Congested | 14.7% | 23.8% | **34.3%** | 45.0% | 55.0% |

The level varies widely, **but the borough ranking is identical at every
threshold tested**: Manhattan, then Brooklyn, then the Bronx, then Queens at 6, 8
and 10 mph.

The KPI should therefore be used **for ranking and for trend, not quoted as an
absolute percentage.** A statement such as "34% of trips are congested" describes
the 8 mph line more than it describes New York. The decision the pipeline
supports, which zones and hours to target, holds regardless of where that line
is drawn.

### C. Unknown zones are codes, not places

Zones 264 and 265 mean "Unknown" and "Outside of NYC". Dropping those trips would
understate citywide volume, while treating them as real zones would add a
phantom borough to every report. Rule V06 therefore excludes them from
*geographic* metrics only, which is why `is_valid_zone` exists separately from
`is_valid_core`.

---

## 7. Why the pipeline is dependable

| Property | How |
|---|---|
| **Idempotent** | Each month is a partition that is removed and rewritten on rerun, so a rerun cannot double-count. A test reprocesses a month and asserts that the metrics are identical. |
| **Raw data immutable** | Downloads are atomic (`.part` then rename), `data/raw/` is never modified, and every input carries a SHA-256. |
| **Fails loudly** | 10 check families (C1–C10) and 8 rule abort limits. Any breach aborts that month and publishes **nothing** for it. |
| **Fails partially** | With `--all`, one bad month does not block the other five, and the run exits non-zero. |
| **Self-reconciling** | C9 asserts that valid plus excluded rows equal the total. C10 recomputes the KPI from the published mart and requires agreement. A trip cannot disappear between raw data and the mart without the run failing. |
| **Auditable** | Every run writes `logs/run_summary_<ts>.json` with per-stage row counts, every check outcome, the duration and the git commit. |
| **Resumable** | `--from-stage` restarts mid-pipeline without re-downloading. |

The pipeline also guards against the specific bugs that silently corrupt
metrics:
- **Join fan-out:** C7 asserts that the row count is unchanged across three LEFT JOINs.
- **Schema drift:** C2.
- **Mislabelled extracts:** C4.
- **Invalid rows defaulting to FALSE:** `is_congested` is NULL outside the valid population, so an invalid trip can never be averaged in as "not congested".

```
$ python -m pytest
30 passed in 8.36s
```

---

## 8. Limitations

These limits apply to every number above:

- **Unmet demand is invisible.** Only *completed* trips are recorded, so a zone
  that drivers avoid can look perfectly healthy.
- **Taxis are a biased sample, not a census.** Trips are concentrated in
  Manhattan and at the airports, and ride-hail (several times taxi volume) is
  absent entirely.
- **There is no route data**, so the pipeline can rank *where* to look but cannot
  explain *why* congestion occurs without fieldwork.
- **Six months cannot separate trend from season.** The January to May rise needs
  12 months of data before it can be called a trend.
- **TLC publishes data about two months late**, which makes this a planning
  instrument rather than a live monitor.

The full register, including the daylight-saving edge case and every assumption,
is in [`docs/known_unknowns.md`](docs/known_unknowns.md).

---

## 9. Documentation

| Document | Contents |
|---|---|
| [`docs/source_map.md`](docs/source_map.md) | Question → information → source; ownership, grain, gaps |
| [`docs/data_model.md`](docs/data_model.md) | Workflow state machine, ER diagram, derived-field choices |
| [`docs/validation_rules.md`](docs/validation_rules.md) | All 8 rules with business rationale |
| [`docs/known_unknowns.md`](docs/known_unknowns.md) | Known / Unknown / Assumption / Limitation |
| [`docs/evidence_table.md`](docs/evidence_table.md) | Final metrics (generated from the marts, never typed by hand) |
