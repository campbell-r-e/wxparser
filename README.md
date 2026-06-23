# wxparser

Continuously listen to the NOAA Weather Radio broadcast from **KJY93 Muncie, IN
(162.425 MHz)**, transcribe it, and save **only new/updated reports** — no duplicates from
the repeating broadcast loop.

Audio is tapped from the line-out of a dedicated weather radio (a Reecom R-1630), so RF
reception is handled in hardware and the entire software stack stays permissively licensed
and MIT-distributable.

**Fully offline:** once installed, wxparser requires no internet — all data comes over the
radio and transcription is local. It keeps working when the internet is down.

> **Status:** planning. See [PLAN.md](PLAN.md) for scope, architecture, and phased
> milestones.

## How it works

NWR replays the same loop of products every few minutes until NWS updates content, and the
audio is deterministic TTS. wxparser fingerprints the audio to detect repeats and runs
speech-to-text **only on genuinely new** segments, so it can run continuously even on modest
hardware.

```
radio line-out → USB audio → capture → VAD → audio-fingerprint novelty gate
  → transcribe novel segments only → text dedup → save new report
```

## License

[MIT](LICENSE)
