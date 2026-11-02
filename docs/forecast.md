# Battery lifetime forecast (Andreas fork)

This fork is based on upstream Battery Notes **3.7.2**. Existing integration,
device, subentry and entity identifiers are preserved. Install **either** this
fork or upstream for the `battery_notes` domain, never both.

## Days until 10%

Percentage-based notes have an optional diagnostic sensor, `battery_days_to_10`.
Enable it in the entity settings for the devices you want to monitor. It is
disabled by default, so installing the fork does not add active forecasts to
every device.

The first **72 hours after replacement** are a settling phase (`settling`).
Their percentage loss is excluded from the forecast, so the initial voltage
settling of freshly charged cells cannot dominate the remaining lifetime.
Battery+ remains the original percentage; this is not a capacity calibration.

After settling, the integration keeps the first valid report per six-hour UTC
slot, at most 60 samples per note. It computes the median for each completed UTC
day and uses the most recent **14 days**. The slope is the median of all pairwise
daily slopes separated by at least two days (Theil-Sen). Each day has equal
weight; frequent reports and a single anomalous day cannot dominate the trend.

A forecast requires at least seven observed days spanning **seven full days
after settling**, with no gap longer than three days. It also requires a positive
trend corresponding to at least **three percentage points** across that span.
Short/missing history gives `insufficient_data`; a flat, recovering or unresolved
trend gives `trend_unclear`, not an extremely large lifetime estimate. There is
no fallback to the old replacement-to-current average. The estimate is
`ceil((current percent - 10) / daily percentage-point loss)`. Consequently it
will not usually become available until 10–12 days after a new replacement.

At 10% or below the result is zero, even during settling. A missing baseline,
unavailable source, or a report older than three days produces an unknown value
and an explanatory `forecast_status`. Low-battery events remain unchanged.
The diagnostic attributes include `daily_loss_percent`, `trend_days`,
`sample_days`, `settling_hours` and `method: daily_median_theil_sen`.
When an MQTT source is `unknown` after startup, the sensor can use its last
persisted measurement with the original timestamp (`reading_source:
stored_report`). An explicitly unavailable source still has no forecast, and
the same three-day freshness limit applies to restored measurements.

The percentage is a device estimate, not a linear measurement of remaining
energy. Temperature, motor use, battery chemistry and initial voltage settling
can change the discharge curve. Treat the result as a rough planning estimate,
not a guaranteed replacement deadline.

## Recording replacement evidence

The existing replacement button and `battery_notes.set_battery_replaced` action
still work. Confirm a current replacement after the new battery percentage has
arrived. The action also accepts:

- `battery_level`: measured percentage immediately after replacement; supply it
  when importing a historical replacement date.
- `estimated`: mark a date inferred from historical measurements.
- `uncertainty_hours`: timestamp precision/uncertainty of that estimate.
- `trend_samples`: optional list of up to 60 measured `time` (ISO timestamp) /
  `level` (0–100 percentage) pairs from the same battery cycle. This can seed the
  trend during a one-time historical import. Invalid values fail validation;
  future/pre-replacement reports are ignored, settling samples are excluded,
  and six-hour sampling/retention limits still apply. A >10-point rise within
  imported evidence suspends the forecast as well. This action replaces the
  baseline/history; preserve the recorded date, initial level and evidence flags
  when importing an existing cycle. No recorder history is fetched by the action.

Historical dates without an initial measurement intentionally have no forecast.
Old dates defaulted by upstream from device creation are not used as evidence.
The timestamp entity exposes the evidence source, uncertainty and initial level.

A subsequent increase of **more than ten percentage points** suspends the
forecast (`replacement_suspected`) until the replacement is confirmed. It does
not silently change the recorded replacement date. A rise can also be a device
reset or voltage recovery.

## Performance and persistence

The forecast does not scan recorder history, poll devices, or call network APIs.
It uses at most 60 persisted samples per evidenced note and existing battery reports.
Enabled forecast sensors check freshness every six hours. State writes occur
only when the whole-day estimate, status or evidence changes. Baseline writes
use the existing batched Battery Notes storage mechanism; new samples use the existing 15-minute report-save delay and at most four new slots
per day. Calculation is bounded by 60 samples and at most 91 daily slope pairs.

Historical reconstruction is a separate one-time operation. Persistent forecast
records use an optional `battery_forecast` field in existing storage entries;
legacy entries load without a migration. Existing baselines without samples
start collecting after upgrade and show `insufficient_data` until enough
post-settling evidence is available or explicitly imported. Upstream 3.7.2
ignores unknown fields,
so rollback preserves existing notes and entities but discards forecast metadata
when upstream next saves its storage. Back up storage before switching versions.

## HACS maintenance

Use the custom integration repository `andreasNfink/HA-Battery-Notes` and this
fork's releases. Unregister/ignore the upstream HACS repository before installing
the fork. Do not remove the configured Battery Notes integration or its devices.

The `upstream` git remote remains `andrew-codechimp/HA-Battery-Notes`. Upstream
updates are reviewed and merged into the fork, tested, then released with a
distinct version. They are not applied automatically over the local extensions.
