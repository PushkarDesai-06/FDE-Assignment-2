# Demo script — 3 to 5 minutes

A running order for the recorded walkthrough. The assignment asks for **one
important FDE judgement call**; this script builds the whole demo around the
V08 decision, because it is the one where the *right* answer and the *instinctive*
answer differ, and the data later proved it.

Keep the terminal and the GitHub repo side by side.

---

## 0:00 — The problem (30s)

> "TLC operations has money for curb management and signal retiming, and no
> defensible way to decide where to spend it. The trip data exists, but it's two
> fleets with incompatible schemas, it contains physically impossible records, and
> it has no weather context — so you can't even ask 'is this congestion or is it
> just raining?'
>
> The KPI I built is the **congested trip rate**: the share of trips averaging
> under 8 mph. The decision it supports is **which zones and hours to target.**"

## 0:30 — Show it runs (45s)

```bash
PYTHONPATH=src python -m pipeline.run --month 2024-03
```

Talk over the output as the stages scroll:

> "Ingest — nothing re-downloads, the manifest checksum matched. Six completeness
> checks. Validation: 3.6M rows, 2.6% excluded. The joins are checked for fan-out.
> Then five metrics. Every check that ran is in a JSON run summary with the git
> commit."

Point out one line: `zone join 100.000%, weather join 100.000%`.

## 1:15 — The headline finding (45s)

Open `notebooks/05_metrics_dashboard.ipynb`, scroll to the Manhattan hourly chart.

> "This is the finding that pays for the project. Everyone assumes rush hour.
> Weekday **midday, 11 to 3, runs 57% congested — 13 points worse than the 5-to-7
> evening peak.** That points enforcement at the middle of the day, which is not
> where an intuition-led programme would start.
>
> And the Queens number — 2.8%, best in the city — is a trap. Queens pickups are
> dominated by JFK and LaGuardia, and airport runs are highway trips at 23 mph.
> It's a composition effect, not free-flowing streets."

## 2:00 — The judgement call (90s) ← **the core of the demo**

Open `docs/known_unknowns.md` at K7, or the README §6A table.

> "Here's the judgement call I want to walk through.
>
> `passenger_count` is null on about 6% of trips. The instinctive move is to treat
> that as dirty data and exclude those rows. I didn't — I made that rule
> **flag-only**, because `passenger_count` feeds **none** of my metrics. My
> reasoning at the time was just: don't pay a 6% cost for a field nobody uses.
>
> Then this happened."

Show the table:

| Jan | Feb | **Mar** | Apr | May | Jun |
|---|---|---|---|---|---|
| 5.8% | 7.3% | **12.9%** | 12.6% | 11.8% | 12.5% |

> "In March the upstream vendor feed changed and the null rate **doubled**.
>
> If I'd made that an exclusion rule, my pipeline would have started throwing away
> 12% of trips in March — and **the KPI would have moved for a reason that has
> nothing to do with traffic.** It would have looked like a real change in
> congestion. Nobody would have found that for months.
>
> Because the rule only flags, the break showed up in the data-quality metric
> where it belongs, and the KPI never moved.
>
> The general principle: **dropping data you don't need isn't free. It silently
> couples your metric to someone else's reporting habits.** That's why nothing in
> this pipeline deletes a row — every rule flags, and each metric declares the
> population it's entitled to use."

## 3:30 — Honesty about the number (45s)

Show the sensitivity table (README §6B).

> "One more thing I'd want a stakeholder to hear. The 8 mph line is my choice, and
> the KPI is *very* sensitive to it — 15% at 6 mph, 55% at 10 mph.
>
> But I checked what actually matters: **the borough ranking is identical at every
> threshold.** Manhattan, Brooklyn, Bronx, Queens — same order at 6, 8 and 10.
>
> So I publish it as: use this for **ranking and for trend, don't quote the
> absolute number.** '34% of trips are congested' is a fact about an 8 mph line,
> not about New York. The decision it supports is robust; the headline figure
> isn't, and saying so is the difference between a metric people can act on and
> one they'll later stop trusting."

## 4:15 — Dependability, briefly (30s)

```bash
python -m pytest
```

> "30 tests. One of them reprocesses a month and asserts the published numbers come
> out identical — that's the rerun guarantee. Others prove a missing file, a volume
> collapse or a schema change **fails the run** instead of quietly publishing a
> smaller number.
>
> I also deleted every derived artefact and rebuilt all six months from raw: the
> metrics came back identical."

## 4:45 — Close (15s)

> "So: two source systems reconciled, three retrieval modes, eight validation rules
> that flag rather than delete, five metrics, and a rerun I'd trust. The output
> tells ops to look at Manhattan midday first — and tells them exactly how much of
> that number they should believe."

---

## If asked: "why didn't you just use one fleet?"

> Green is only 1.6% of volume, but it's the only fleet serving the outer boroughs,
> and it has a genuinely different schema — `lpep_` instead of `tpep_`, extra
> columns, a missing airport fee. Reconciling it is real source work, and it's what
> makes the borough comparison meaningful rather than a Manhattan-only view.

## If asked: "is the rise from January real?"

> I wouldn't call it a trend yet. Six months can't separate trend from season — the
> Jan-to-May rise is equally consistent with recovery from winter lows. That's
> limitation L4, and the fix is twelve months, which is a config change and a
> rerun.
