# Third-party components

NextAI Platform downloads the following components **on the target PC at install time**; none of them are
bundled in the installer. Each is used under its own license.

## Runtimes

| Component | Source | License |
|---|---|---|
| llama.cpp (`llama-server`) | github.com/ggml-org/llama.cpp releases | MIT |
| stable-diffusion.cpp (`sd`) | github.com/leejet/stable-diffusion.cpp releases | MIT |
| CPython for WASI (`python.wasm`) | github.com/vmware-labs/webassembly-language-runtimes | Apache-2.0 / PSF |
| CPython 3.12 (via uv / python-build-standalone) | github.com/astral-sh/python-build-standalone | PSF |
| uv | pypi.org/project/uv | MIT / Apache-2.0 |
| PyTorch + Transformers (music, optional) | download.pytorch.org, pypi.org | BSD-3 / Apache-2.0 |

## Python packages (hash-pinned in `server/requirements.lock`)

FastAPI (MIT), Starlette (BSD-3), Uvicorn (BSD-3), Pydantic (MIT), argon2-cffi (MIT), psutil (BSD-3),
httpx/httpcore (BSD-3), python-multipart (Apache-2.0), cryptography (Apache-2.0/BSD), nvidia-ml-py (BSD),
wasmtime-py (Apache-2.0), pypdf (BSD-3), Pillow (MIT-CMU), and their dependencies.

## Models (downloaded from Hugging Face)

| Model | License | Note |
|---|---|---|
| Qwen3 4B / 30B-A3B / Coder 30B-A3B, Qwen3 Embedding, Qwen2.5-VL 7B | Apache-2.0 | |
| gpt-oss-20b (optional full set) | Apache-2.0 | |
| FLUX.1 schnell | Apache-2.0 | |
| Wan2.1 T2V 1.3B | Apache-2.0 | |
| umt5-xxl encoder / CLIP-L / T5-XXL encoders | Apache-2.0 / MIT | |
| MusicGen small | **CC-BY-NC-4.0** | **Non-commercial use only.** Remove the model from the admin console if this does not fit your use. |

Model GGUF conversions are provided by their respective publishers (Qwen, unsloth, bartowski, ggml-org,
second-state, city96, Comfy-Org). Verify the license of any custom model you add.
