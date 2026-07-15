# Station profiles

A **profile** holds the only things that differ between NWR deployments. Everything
else — the capture/STT/voting pipeline, the SAME/EAS decoder (national FIPS + event
tables), the extraction patterns, the API — is generic and needs no changes.

Select a profile with `WX_PROFILE`:

- a bundled name → `WX_PROFILE=kjy93_muncie` loads `profiles/kjy93_muncie.json`
- a path → `WX_PROFILE=/etc/wxparser/wny.json`

Defaults to `kjy93_muncie` (KJY93 / east-central Indiana, what this project was
developed against).

## Format

```json
{
  "station": "KJY93",
  "frequency_mhz": 162.425,
  "primary_city": "Muncie",
  "stt_prompt": "NOAA Weather Radio for <region>. Counties: ... Towns: ...",
  "place_corrections": {
    "CanonicalCity": ["MisheardVariant", "AnotherVariant"]
  }
}
```

| Key | What it is |
|---|---|
| `station` | NWR callsign (overridable with `WX_STATION`) |
| `frequency_mhz` | your local NWR channel, 162.400–162.550 (`WX_FREQ_MHZ`) |
| `primary_city` | the home/station city — standalone obs attach here (`WX_PRIMARY_CITY`) |
| `stt_prompt` | whisper vocabulary prompt: your NWS office's counties + towns (~50 tokens, keep it short) |
| `place_corrections` | canonical city → the way whisper mishears it, for your coverage cities |

## Porting to a new region

1. **Hardware**: a weather radio tuned to your local NWR frequency, audio into the PC.
2. Copy `kjy93_muncie.json` → `<yourstation>.json`; set `station`, `frequency_mhz`,
   `primary_city`.
3. Replace `stt_prompt` with your forecast office's counties and towns.
4. Seed `place_corrections` with your coverage cities (canonical spellings; you can
   start with empty variant lists).
5. Set `WX_PROFILE=<yourstation>` and run.
6. Let it collect, then run `deploy/propose_corrections.py` to mine your station
   voice's actual mishearings, fold the safe ones into the profile, and
   `python -m wxparser.reprocess` to apply them retroactively.
