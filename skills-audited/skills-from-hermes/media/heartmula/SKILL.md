---
name: heartmula
description: "HeartMuLa: Suno-like song generation from lyrics + tags."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [music, audio, generation, ai, heartmula, heartcodec, lyrics, songs]
    related_skills: [audiocraft]
---

# HeartMuLa - Open-Source Music Generation

Open-source (Apache-2.0) music foundation model that generates full songs from
lyrics + tags, multilingual, comparable to Suno. Assumes the environment is
already set up — see [references/installation.md](references/installation.md)
for clone/venv/patch steps if `heartlib` isn't installed yet.

## When to Use
- User wants to generate music/songs from text descriptions
- User wants an open-source Suno alternative or local/offline music generation
- User asks about HeartMuLa, heartlib, or AI music generation

## Hardware Requirements

| Scenario | Setting |
| --- | --- |
| Minimum | 8GB VRAM with `--lazy_load true` (loads/unloads models sequentially) |
| Recommended | 16GB+ VRAM, single GPU |
| Multi-GPU | `--mula_device cuda:0 --codec_device cuda:1` |
| 3B model + lazy_load | peaks at ~6.2GB VRAM |

No NVIDIA GPU: use `--mula_device cpu --codec_device cpu` (extremely slow,
30-60+ min/song, needs ~12GB+ RAM) or point the user to a cloud GPU / the
online demo at https://heartmula.github.io/ instead.

## Usage

```bash
cd heartlib
. .venv/bin/activate
python ./examples/run_music_generation.py \
  --model_path=./ckpt \
  --version="3B" \
  --lyrics="./assets/lyrics.txt" \
  --tags="./assets/tags.txt" \
  --save_path="./assets/output.mp3" \
  --lazy_load true
```

RTF (Real-Time Factor) ≈ 1.0 — a 4-minute song takes ~4 minutes to generate on
GPU. Output: MP3, 48kHz stereo, 128kbps.

## Input Formatting

**Tags** (comma-separated, no spaces): `piano,happy,wedding,synthesizer,romantic`

**Lyrics** (bracketed structural tags):
```
[Intro]

[Verse]
Your lyrics here...

[Chorus]
Chorus lyrics...

[Bridge]
Bridge lyrics...

[Outro]
```

## Key Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--max_audio_length_ms` | 240000 | Max length in ms (240s = 4 min) |
| `--topk` | 50 | Top-k sampling |
| `--temperature` | 1.0 | Sampling temperature |
| `--cfg_scale` | 1.5 | Classifier-free guidance scale |
| `--lazy_load` | false | Load/unload models on demand (saves VRAM) |
| `--mula_dtype` | bfloat16 | Dtype for HeartMuLa (bf16 recommended) |
| `--codec_dtype` | float32 | Dtype for HeartCodec (fp32 recommended for quality) |

## Pitfalls
1. **Do NOT use bf16 for HeartCodec** — degrades audio quality. Use fp32 (default).
2. **Tags may be ignored** — known issue (#90). Lyrics tend to dominate; experiment with tag ordering.
3. **Triton not available on macOS** — Linux/CUDA only for GPU acceleration.
4. **RTX 5080 incompatibility** reported in upstream issues.
5. If generation fails on a fresh checkout, the dependency/patch steps in
   [references/installation.md](references/installation.md) likely weren't
   applied yet.
