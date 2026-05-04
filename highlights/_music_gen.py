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

    # MusicGen audio token rate: 50 tokens/second at 32 kHz with stride 640
    max_new_tokens = int(duration * 50) + 4

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

    print(f"[music] Generating {duration:.0f}s soundtrack: {prompt!r}")
    sys.stdout.flush()

    with torch.no_grad():
        audio_values = model.generate(**inputs, max_new_tokens=max_new_tokens)

    sampling_rate = model.config.audio_encoder.sampling_rate
    audio = audio_values[0, 0].cpu().numpy()
    audio_int16 = (audio * 32767).clip(-32768, 32767).astype(np.int16)

    scipy.io.wavfile.write(output, sampling_rate, audio_int16)
    print(f"[music] Saved: {output}")


if __name__ == "__main__":
    main()
