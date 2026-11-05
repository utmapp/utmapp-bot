# <Topic> — handoff <yyyy-mm-dd>

**Status:** <one line: root-caused / fix in review / open, narrowed to X>
**Symptom:** <what the user sees, with numbers>

## Repro

```sh
# UTM build, VM, backend, driver package
python3 <plugin>/scripts/utmbot vm up --backend <…> --tag <…>
python3 <plugin>/scripts/utmbot install <package>
python3 <plugin>/scripts/utmbot test <probe or app> …
```

Expected: <…>  Actual: <…>  (run dir: <…>)

## Environment

| | |
|---|---|
| Mac / macOS | |
| UTM app | path, version, `verify-app.sh` markers |
| Backend / D3DMetal | `utmbot backend` output |
| Guest | OS build, driver version |

## Trees

| Repo | Branch @ commit | Uncommitted | Notes |
|---|---|---|---|
| | | none / diagnostics (patch: …) / fix-in-progress | |

## Confirmed findings

1. <finding> — evidence: <log line / probe verdict / measurement / commit>

## Hypotheses (unproven)

- <hypothesis> — would be proven by <…>, killed by <…>

## Eliminated — do not redo

- <idea>: <evidence that killed it>

## Next step

<one concrete action, and what each possible result means>
