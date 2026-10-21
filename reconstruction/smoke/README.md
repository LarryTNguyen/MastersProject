# Astronomy Shop payment-failure smoke test

The smoke test uses the upstream OpenTelemetry Astronomy Shop, pinned to a tag
or commit, and injects the built-in `paymentFailure` flag at `100%`. It keeps a
private flag configuration so the upstream checkout is never edited.

The controller covers four repeatable actions:

1. `prepare` creates a shared run ID, a healthy flag configuration, a Compose
   override, and a ground-truth manifest.
2. `inject` switches `paymentFailure` to `100%` and records the injection time.
3. `clear` disables the fault and records the end of the fault window.
4. `verify` checks captured canonical JSONL for the required healthy/faulty
   checkout traces, connected/non-invalid topology, and Payment error evidence.

## Procedure

From `reconstruction`:

```powershell
python smoke/astronomy_shop.py prepare `
  --demo-dir C:/path/to/opentelemetry-demo `
  --demo-version <pinned-tag-or-commit>
```

The output includes `run_id`, `flag_config`, and `compose_override`. Set the
absolute flag path before starting the demo, and include the generated override
after the upstream Compose files:

```powershell
$env:STA_FLAG_CONFIG = (Resolve-Path .astronomy-smoke/demo.flagd.json)
docker compose --env-file .env --env-file .env.override `
  -f compose.yaml -f compose.full.yaml -f compose.observability.yaml `
  -f C:/path/to/MastersProject/reconstruction/.astronomy-smoke/astronomy-smoke.override.yaml `
  up --force-recreate --remove-orphans --detach
```

Configure the local collector/normalizer to assign the printed run ID to every
canonical span in this experiment. Keep every failure flag off initially.

Generate checkout traffic for at least ten seconds and until five successful
checkout traces exist. Then inject the fault:

```powershell
python smoke/astronomy_shop.py inject
```

The mounted file may be watched automatically by flagd. To remove version-
specific ambiguity, restart only flagd after each change using the same Compose
file list:

```powershell
docker compose <same -f arguments> restart flagd
```

Continue the identical checkout workload for at least twenty seconds and until
five failed checkout traces exist. Then clear the flag, restart flagd, wait for
the five-second quiet window, and signal end-of-input:

```powershell
python smoke/astronomy_shop.py clear
python -m trace_reconstruction finish <run-id>
```

Export the captured canonical records as JSONL and verify them:

```powershell
python smoke/astronomy_shop.py verify --input path/to/capture.jsonl
```

The check intentionally validates trace evidence rather than a top-three
ranking; ranking is Sprint 4 work. The runtime directory is disposable and
should not be committed.
