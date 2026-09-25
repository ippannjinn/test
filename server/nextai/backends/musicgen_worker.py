"""Standalone MusicGen worker. Runs inside the optional `genai` PyTorch environment.

Prints JSON progress lines on stdout: {"progress": 0.5, "message": "..."}.
"""
import argparse
import json
import sys


def emit(p, msg=""):
    print(json.dumps({"progress": p, "message": msg}), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cpu", action="store_true")
    a = ap.parse_args()

    emit(0.02, "loading")
    import numpy as np
    import scipy.io.wavfile
    import torch
    from transformers import AutoProcessor, MusicgenForConditionalGeneration

    device = "cuda" if torch.cuda.is_available() and not a.cpu else "cpu"
    dtype = torch.float16 if device == "cuda" else torch.float32
    torch.manual_seed(a.seed)
    processor = AutoProcessor.from_pretrained(a.model_dir, local_files_only=True)
    model = MusicgenForConditionalGeneration.from_pretrained(a.model_dir, local_files_only=True, torch_dtype=dtype)
    model.to(device)
    emit(0.3, f"generating on {device}")
    inputs = processor(text=[a.prompt], padding=True, return_tensors="pt").to(device)
    tokens = int(max(1.0, min(a.seconds, 60.0)) * 50)
    with torch.inference_mode():
        audio = model.generate(**inputs, do_sample=True, guidance_scale=3.0, max_new_tokens=tokens)
    emit(0.9, "writing")
    rate = model.config.audio_encoder.sampling_rate
    data = audio[0, 0].float().cpu().numpy()
    data = np.clip(data, -1.0, 1.0)
    scipy.io.wavfile.write(a.out, rate, (data * 32767).astype(np.int16))
    emit(1.0, "done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
