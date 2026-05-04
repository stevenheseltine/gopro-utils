#!/usr/bin/env python3
"""
Called as a subprocess by highlights.py to generate a music track using MusicGen.
Runs under Python 3.12 where torch/transformers are available.

Usage: python3.12 _music_gen.py <duration> <prompt> <model_size> <output_wav_path>
"""

import sys

def main() -> None:
    if len(sys.argv) != 5:
        print(f"Usage: {sys.argv[0]} <duration> <prompt> <model_size> <output_path>")
        sys.exit(1)

    duration   = float(sys.argv[1])
    prompt     = sys.argv[2]
    model_size = sys.argv[3]
    output     = sys.argv[4]

    # MusicGen positional embedding table is fixed at ~2048 entries (~40s).
    # Generate in 30-second chunks and concatenate to support longer reels.
    CHUNK_TOKENS = 1500  # ~30 seconds per pass, safely inside the position limit
    total_tokens = int(duration * 50)

    print(f"[music] Loading facebook/musicgen-{model_size} — first run downloads the model")
    sys.stdout.flush()

    import torch
    import scipy.io.wavfile
    import numpy as np
    from transformers import AutoProcessor, MusicgenForConditionalGeneration

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model_id = f"facebook/musicgen-{model_size}"

    # use_safetensors=True avoids torch.load CVE-2025-32434 restriction (requires torch>=2.6 for .bin)
    processor = AutoProcessor.from_pretrained(model_id)
    model = MusicgenForConditionalGeneration.from_pretrained(model_id, use_safetensors=True).to(device)

    inputs = processor(text=[prompt], padding=True, return_tensors="pt").to(device)

    n_chunks = -(-total_tokens // CHUNK_TOKENS)  # ceiling division
    print(f"[music] Generating {duration:.0f}s soundtrack in {n_chunks} chunk(s): {prompt!r}")
    sys.stdout.flush()

    chunks = []
    remaining = total_tokens
    for i in range(n_chunks):
        tokens = min(remaining, CHUNK_TOKENS)
        print(f"[music] Chunk {i + 1}/{n_chunks} ({tokens // 50}s)")
        sys.stdout.flush()
        with torch.no_grad():
            audio_values = model.generate(**inputs, max_new_tokens=tokens)
        chunks.append(audio_values[0, 0].cpu().numpy())
        remaining -= tokens

    sampling_rate = model.config.audio_encoder.sampling_rate
    audio = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
    audio_int16 = (audio * 32767).clip(-32768, 32767).astype(np.int16)

    scipy.io.wavfile.write(output, sampling_rate, audio_int16)
    print(f"[music] Saved: {output}")


if __name__ == "__main__":
    main()
