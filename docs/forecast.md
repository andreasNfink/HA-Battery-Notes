# Battery runtime forecast (local fork)

Enable the optional `battery_days_to_10` diagnostic sensor for each desired note.
It estimates days until **10%** on the same percentage scale as Battery+.

## NiMH percentage correction

In a battery note's reconfiguration form, select **Percentage correction →
NiMH / LSD-NiMH** for rechargeable AA/AAA cells in devices calibrated for alkaline
batteries. The default remains **Unchanged** for all existing and new notes.
The optional additional settings are `nimh_full_level` (default **83**) and
`nimh_settled_level` (default **77**), with `0 < settled < full <= 100`.

The piecewise-linear calibration is **0 → 0%, 77 → 97%, 83 → 100%**.
Higher valid reports are capped at 100%; the initial six raw percentage points
only account for three displayed points. Below the settled level the display is
linear to zero. These are provisional calibration values, not measured capacity:
NiMH's flat voltage plateau, temperature, load and the device's own conversion
prevent a precise state-of-charge estimate. Do not apply this profile to lithium
cells or to devices already reporting an appropriate rechargeable-cell percentage.
See the [Energizer NiMH handbook](https://data.energizer.com/wp-content/uploads/2020/11/nimhhandbook_ver2-2.pdf).

Battery+, numeric low-battery thresholds and forecasts use corrected percentages,
including a forecast target of **10% corrected**. Low-battery templates retain
their explicit precedence. Templates that generate a percentage run before this
correction; do not also put the same correction in the template. Binary low-only
devices cannot provide a percentage to calibrate.

Source entities, stored last-reported levels, replacement start levels, imported
history and compacted samples remain **raw**. The forecast transforms that small
history on read, so enabling, changing or disabling a profile neither mixes scales
nor invents replacements. Supply raw values to history/replacement actions.
Replacement/increase detection continues to compare raw percentages, even when
both displayed values would be clamped at 100%. Battery+ exposes
`raw_battery_level`, `battery_percentage_profile`, `nimh_full_level` and
`nimh_settled_level` for inspection. Existing history is not rewritten.

## Evidence and smoothing

The first **72 hours after replacement** are excluded, so the initial voltage
settling cannot dominate the forecast. After that, at least **three full days**
of observed history are required. Daily medians and the median of pairwise slopes
(Theil-Sen) smooth quantisation and isolated anomalous days. A trend must explain
at least **three percentage points** of decline. A short trend is marked
`trend_quality: provisional`; from seven days it is `established`.

For the **NiMH profile**, the configured settled source level (77 by default)
replaces the fixed waiting period. Fit only samples at or below that knee. At
least **24 hours** between daily representatives and **one corrected percentage
point** of fitted decline are required, using the partially observed current day
as well. Short trends remain provisional. The compressed upper segment has a
different slope and must not be extrapolated all the way down to 10%.

## Fallback estimates and unchanged observations

Use the own fitted trend first. If it is unresolved or contains gaps, use a
clearly labelled **average of this battery's observed decline**: the first
observed day's median to the current value, over at least three days and one
corrected point of decline, after the applicable settling exclusion. This can
span observation gaps; it does not invent intermediate measurements or claim a
robust fitted trend. Its status is `average_estimate`, quality `average` and
`estimate_source: own_average`.

If that is unavailable too, use a clearly labelled **reference estimate** from
at least three independent other devices. Match manufacturer, model/model ID,
cell type and quantity, and the entire percentage profile. Duplicate notes for
one physical device have one vote; the target device never predicts itself.
Each reference needs at least seven days and three corrected points of observed
main-discharge decline. The median of their individual average rates resists
extreme devices. This is observed partial-cycle experience, not a measured
complete battery lifetime. Status is `reference_estimate`, quality `reference`,
`estimate_source: similar_devices`; `reference_devices` gives the sample size.
The current corrected level minus 10%, divided by that rate, gives the days.
New source reports and reference updates replace the fallback automatically.

An unavailable source, stale stored report or suspected replacement still blocks
all methods. A source awaiting its first MQTT state can reuse the actual stored
report for up to three days, preserving its age.

Enabled forecast sensors also observe the currently available HA percentage at
their existing six-hour timer. Thus a constant 65% remains an observed plateau
even if the source integration suppresses unchanged writes. These are **HA state
observations**, not newly requested physical measurements. They never refresh
`battery_last_reported`, change replacement dates or fire battery events.
Unknown/unavailable states add no samples. Samples remain bounded and raw;
fitting applies settling/knee rules on read, so changing profiles does not discard
the early raw observations. History imports can fill earlier gaps explicitly
from existing source history; runtime estimation never queries recorder.

Prefer the last **14 days**; if discharge is too slow to resolve, extend to 30
and then **90 days**. Missing days are not interpolated. Gaps longer than three
days invalidate the corresponding window. The estimate is
`ceil((current percentage - 10) / daily percentage-point loss)`.
A plateau, including a sensor still reporting 100%, cannot supply a meaningful
day count and stays `trend_unclear`. At or below 10% the estimate is zero.
Missing/unavailable sources and stored reports older than three days remain unknown.
An MQTT source awaiting its first report may reuse a real persisted reading with
its original timestamp (`reading_source: stored_report`).

A known replacement is useful but **not necessary** for a discharge trend.
`import_battery_history` can establish an observation-only anchor without
changing the replacement date or firing a replacement event. The first 72 hours
of that observation are excluded conservatively as well. The attributes expose
`replacement_known: false` and `observation_started`; an unknown replacement date
is never invented from device creation or the first observed percentage.

## Replacement and historical import

The replacement button and `set_battery_replaced` still work. Optional fields are
`battery_level` (measured immediately after replacement), `estimated`,
`uncertainty_hours`, and `trend_samples` (at most **140** timestamp/level pairs).
When importing an existing known cycle, preserve its date and evidence flags.
The action resets the old cycle, and never treats today's percentage as the
historical initial level. Future and previous-cycle points are ignored.

Use `import_battery_history` to merge samples without changing replacement
metadata. It accepts `device_id` or `source_entity_id`, `trend_samples`, and an
optional `detect_replacements` boolean. Samples must belong to one battery cycle;
a >10-point rise in imported data suspends the forecast instead of mixing cycles.
The action does not read recorder history itself.

By default a later rise of **more than ten percentage points** only suspends the
forecast (`replacement_suspected`), preserving upstream behavior. For notes
explicitly opted in using `detect_replacements: true`, the same rise records a
new **estimated** replacement at the actual report time, clears the old trend,
and starts a fresh 72-hour settling period. Manual replacement confirmations
remain supported and preserve the opt-in. Exactly ten points is not a replacement.
Voltage recovery or a device reset can resemble a replacement; inferred dates
remain estimates.

## Performance and persistence

There are no runtime recorder scans, network calls or extra device polling.
Keep the first available-state observation per six-hour UTC slot for the most recent 15 days and one
observed value per older day, at most **140 records per note** and about 91 days.
Daily medians weight each day equally. A fit considers at most 90 daily points
and about 4,000 slopes, with a short window tried first. Reports and samples use
existing batching and throttling; freshness checks run every six hours. Entity
writes occur only when the estimate or diagnostic evidence changes.

History reconstruction is a separate one-time operation. Persisted data lives
in the existing optional `battery_forecast` dictionary. Old notes and baselines
remain readable. Back up storage before rollback: upstream ignores forecast
fields and may discard them on its next save.

## HACS maintenance

Use `andreasNfink/HA-Battery-Notes` and this fork's releases. Keep the upstream
HACS repository unregistered/ignored; retain configured Battery Notes entities.
Upstream changes are reviewed and merged into the fork before releasing.
