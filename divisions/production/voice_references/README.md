# Voice Reference Files

Place `{commander}.wav` files here to enable XTTS v2 voice cloning.

## Recording Requirements

| Parameter | Specification |
|-----------|---------------|
| **Format** | WAV (16-bit PCM) |
| **Sample Rate** | 22050 Hz or 24000 Hz |
| **Channels** | Mono |
| **Duration** | 5-30 seconds of clean, clear speech |
| **Bit Depth** | 16-bit |

> XTTS v2 resamples internally, but providing audio at 22050 Hz or 24000 Hz
> avoids unnecessary quality loss from resampling artifacts.

## Tips for Good Reference Recordings

- **Quiet room** — record in a space with minimal echo and no background noise.
- **Natural speaking pace** — speak at the tempo you want the TTS to replicate. Avoid rushing or exaggerated slowness.
- **Avoid music/background** — no background music, TV, fans, or other ambient sound. The model needs a clean vocal signal.
- **Consistent microphone distance** — keep ~15-20 cm from the mic. Avoid plosives (use a pop filter if available).
- **Representative content** — speak lines similar in tone and emotion to what the commander would say in-game. The model learns timbre and cadence from the reference.
- **No post-processing** — do not apply compression, EQ, reverb, or noise reduction. Raw, clean audio is best.

## Commander Voice Directory

| Commander | Division | Voice Character | Recommended Tone |
|-----------|----------|-----------------|------------------|
| VAEL | Opportunity | Young ranger, alert | Quick, observant, slightly eager |
| SEREN | Trading | Calm oracle | Measured, precise, contemplative |
| KAELEN | Dev Auto | Gruff forge knight | Deep, methodical, confident |
| LYRIN | Personal | Gentle cleric | Warm, caring, steady |
| ZETH | Op-Sec | Shadow assassin | Low, terse, controlled |
| LYKE | Production | Master artificer | Bold, creative, commanding |

## Usage

Place reference WAV files named after each commander in this directory:

```
divisions/production/voice_references/
  vael.wav
  seren.wav
  kaelen.wav
  lyrin.wav
  zeth.wav
  lyke.wav
```

## How It Works

When `voice_generate.py` synthesizes a voice line for a commander, it checks for
`{commander}.wav` in this directory:

- **Reference found** — uses XTTS `tts_with_vc()` for voice cloning, reproducing the timbre and style of the reference recording.
- **No reference found** — falls back to the built-in `Claribel Dervla` speaker, a generic English voice from the XTTS v2 model.

## Output Location

Generated audio is written to:

```
mobile/assets/generated/voice/{commander}/{queue_id}.wav
```
