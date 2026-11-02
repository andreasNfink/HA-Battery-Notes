# Battery lifetime forecast (Andreas fork)

This fork is based on upstream Battery Notes **3.7.2**. Existing integration,
device, subentry and entity identifiers are preserved. Install **either** this
fork or upstream for the `battery_notes` domain, never both.

## Days until 10%

Percentage-based notes have an optional diagnostic sensor, `battery_days_to_10`.
Enable it in the entity settings for the devices you want to monitor. It is
disabled by default, so installing the fork does not add active forecasts to
every device.

The estimate is `(current percent - 10) × elapsed days / lost percentage points`.
It uses the measured percentage at replacement, never an assumed 100%. Seven
days and at least five lost percentage points are required. At 10% or below the
result is zero. A missing baseline, unavailable source, or a report older than
three days produces an unknown value and an explanatory `forecast_status`.

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

Historical dates without an initial measurement intentionally have no forecast.
Old dates defaulted by upstream from device creation are not used as evidence.
The timestamp entity exposes the evidence source, uncertainty and initial level.

A subsequent increase of **more than ten percentage points** suspends the
forecast (`replacement_suspected`) until the replacement is confirmed. It does
not silently change the recorded replacement date. A rise can also be a device
reset or voltage recovery.

## Performance and persistence

The forecast does not scan recorder history, poll devices, or call network APIs.
It uses a small persisted record per note and existing battery reports.
Enabled forecast sensors check freshness every six hours. State writes occur
only when the whole-day estimate, status or evidence changes. Baseline writes
use the existing batched Battery Notes storage mechanism.

Historical reconstruction is a separate one-time operation. Persistent forecast
records use an optional `battery_forecast` field in existing storage entries;
legacy entries load without a migration. Upstream 3.7.2 ignores unknown fields,
so rollback preserves existing notes and entities but discards forecast metadata
when upstream next saves its storage. Back up storage before switching versions.

## HACS maintenance

Use the custom integration repository `andreasNfink/HA-Battery-Notes` and this
fork's releases. Unregister/ignore the upstream HACS repository before installing
the fork. Do not remove the configured Battery Notes integration or its devices.

The `upstream` git remote remains `andrew-codechimp/HA-Battery-Notes`. Upstream
updates are reviewed and merged into the fork, tested, then released with a
distinct version. They are not applied automatically over the local extensions.
