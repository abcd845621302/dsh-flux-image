# dsh-flux-image

![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey.svg)
[![CI](https://github.com/abcd845621302/dsh-flux-image/actions/workflows/ci.yml/badge.svg)](https://github.com/abcd845621302/dsh-flux-image/actions/workflows/ci.yml)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

A local FLUX text-to-image / reference-image editing tool for **[DeepSeek Harness](https://github.com/deepseek-ai)** (DSH), with automatic VRAM timesharing between local LLMs and ComfyUI.

[中文文档](README.zh-CN.md) | [MIT License](LICENSE) | [Contributing](CONTRIBUTING.md)

![Preview](docs/preview.png)

---

## Overview

`dsh-flux-image` registers a single `generate_image` tool inside DSH. It runs **FLUX.1** models through a local **ComfyUI** instance and automatically frees VRAM from any loaded local LLM (e.g. a Strata/Qwen server) before rendering, then reloads that LLM in the background afterwards.

It is designed for machines where a local LLM and an image model **cannot reside in VRAM at the same time** — e.g. a 12 GB GPU running a 75 GB MoE LLM plus a 12B FLUX model.

Everything runs **locally**: no cloud API, no telemetry, no data leaves your machine.

## Features

| Feature | Description |
|---|---|
| **Text-to-image** | FLUX.1 Krea dev via ComfyUI (default 25 steps) |
| **Reference-image editing** | FLUX.1 Kontext dev (GGUF or safetensors) — keeps character identity |
| **Accelerated mode** | FLUX Turbo/Lightning LoRA, 8-step sampling (≈20% of the usual time, slight quality loss) |
| **VRAM timesharing** | Unloads any configured local LLM (`POST /v1/unload`), renders, then the plugin reloads it in the background so the image is returned first |
| **Dynamic discovery** | Workspace, ComfyUI path/port, model files, and local-LLM endpoint are all auto-detected; environment variables can override |
| **Automatic UI** | Opens the ComfyUI page on first use (window-title detection + 20-minute throttle), closes the ComfyUI it started to free VRAM |
| **Clean lifecycle** | Never leaves stray ComfyUI processes; optionally shows the ComfyUI console window for progress logs |

## How it works

```
Model call: generate_image(prompt, image?, accelerate?, size?, steps?, seed?)
        │
        ▼
  flux-image-plugin/dsh/index.js  (thin DSH wrapper)
        │  spawn `py gen.py ...`
        ▼
  gen.py
    1. detect DSH workspace (DSH_WORKSPACE → workspace.json + DSH_SESSION_ID → default → cwd)
    2. detect ComfyUI install dir / port, scan models (Kontext / Krea / CLIP / VAE / LoRA)
    3. detect local LLM service (STRATA_URL / local_models.json — any health/unload/load URLs / common ports for Strata-compatible services)
    4. if the local LLM is loaded → POST /v1/unload
    5. start ComfyUI if not running (writable dirs redirected into the workspace)
    6. submit the workflow, poll /history, download the PNG to <workspace>/flux_images/
    7. /free ComfyUI → kill the ComfyUI it started
    8. the plugin (index.js) starts reload_strata.py which calls POST /v1/load — the image returns first and the LLM recovers in the background
        │
        ▼
  Returns the absolute path of the saved PNG
```

## Requirements

| Requirement | Notes |
|---|---|
| **Operating system** | **Windows 10/11** — `gen.py` uses `taskkill`, `CREATE_NEW_CONSOLE` and the Windows API for window detection |
| **Python 3** | Invoked as `py` by default; override with `FLUX_IMAGE_PY` |
| **ComfyUI** | Windows portable build containing `ComfyUI/main.py` and `python_embeded/python.exe` |
| **FLUX models** | In ComfyUI `models/diffusion_models`: Krea (text-to-image) and/or Kontext (reference editing) |
| **Text encoders / VAE** | `clip_l`, `t5xxl`, `ae.safetensors` (standard FLUX ComfyUI setup) |
| **Accelerator LoRA** *(optional)* | A file in `models/loras` whose name contains `turbo` or `lightning` |
| **DeepSeek Harness** | The `dsh` desktop app / CLI |

### Model license note

FLUX.1 models are published under their own licenses (FLUX.1 [dev] / [kontext] dev are **non-commercial** licenses). This project only orchestrates ComfyUI — **you are responsible for complying with the licenses of the models you run**.

## Installation

1. Put `gen.py`, `local_models.json`, `flux_dev_fp8_api.json`, `flux_kontext_gguf_api.json` and the
   `flux-image-plugin/` folder together in one plugin directory, e.g. `D:/ComfyUI-Plugin` (any path works).
   The plugin locates `gen.py` through the `FLUX_IMAGE_PLUGIN_DIR` environment variable and then the
   parent folders of the installed package — so either set `FLUX_IMAGE_PLUGIN_DIR` or copy the four
   files next to the installed plugin package.
2. Install `flux-image-plugin/` into DSH, either with the built-in plugin manager:

   ```
   plugin_manager(action: "install_bundle", target: "<path>/flux-image-plugin")
   ```

   or by copying it into `~/.dsh/profiles/<profile>/node_modules/dsh-flux-image` and restarting DSH.
3. Restart DSH (required when `index.js` changes; `gen.py` / workflow JSON changes are picked up on every call).

## Usage

```json
generate_image(prompt, image?, accelerate?, size?, steps?, seed?)
```

| Parameter | Description |
|---|---|
| `prompt` | Image description (FLUX prefers detailed English prompts) |
| `image` | Optional reference-image path → uses the Kontext reference workflow |
| `accelerate` | Optional `true` → load the accelerator LoRA and sample in 8 steps |
| `size` | Text-to-image size, default `1024x1024` |
| `steps` | Sampling steps (defaults: text-to-image 25 / accelerate 8 / Kontext 20) |
| `seed` | Optional random seed |

Generated images are saved to `<DSH workspace>/flux_images/`.

### Examples

```
# Text-to-image
generate_image(prompt="a cute blue cat on a desk, anime style, soft background")

# Accelerated (8 steps)
generate_image(prompt="a cute anime girl icon, blue hair, hanfu", accelerate=true)

# Reference-image editing (Kontext)
generate_image(image="D:/refs/character.jpg",
               prompt="turn the reference into a clean app icon, keep the hair and outfit, no text")
```

## Model selection (dynamic)

### With a reference image

`generate_image(prompt, image="...")` uses the **Kontext** reference workflow. Model resolution:
`FLUX_KONTEXT_MODEL` env → scan `models/diffusion_models` for a file containing `kontext`.

### Without a reference image (text-to-image)

Resolution order:

1. `text2img.json`, then env vars (`FLUX_TEXT2IMG_MODEL`, `FLUX_TEXT2IMG_WORKFLOW`, …);
2. auto-detect: files in `models/diffusion_models` → **FLUX** workflow; files in `models/checkpoints` → **SDXL / SD1.5** workflow (`sdxl_api.json`);
3. default: Krea first, then any FLUX, then any SDXL, then any SD1.5.

### text2img.json (optional)

`text2img.json.example` is provided — copy it to `text2img.json` and adjust:

```json
{
  "model": "sd_xl_base_1.0.safetensors",
  "workflow": "sdxl_api.json",
  "steps": 25,
  "cfg": 7.0,
  "sampler": "dpmpp_2m",
  "scheduler": "karras",
  "size": "1024x1024"
}
```

### Workflow requirements

The workflow must be a ComfyUI **API-format** JSON. The plugin injects the model, prompt, size, steps and seed (and CFG/sampler/scheduler from the config). FLUX workflows use `UNETLoader` / `UnetLoaderGGUF`; SDXL and SD1.5 workflows use `CheckpointLoaderSimple`. A mismatched model/workflow pair is rejected with a clear error.

| Mode | Required ComfyUI nodes |
|---|---|
| FLUX text-to-image | `UNETLoader` (safetensors) or `UnetLoaderGGUF` (GGUF — needs **ComfyUI-GGUF**) |
| SDXL / SD1.5 | `CheckpointLoaderSimple` |
| Kontext | `FluxKontextImageScale`, `ReferenceLatent` (+ `UnetLoaderGGUF` for GGUF models) |

### Model directories

| Model type | Directory |
|---|---|
| FLUX (UNET / GGUF) | `ComfyUI/models/diffusion_models` |
| SDXL / SD1.5 checkpoint | `ComfyUI/models/checkpoints` |

If the local LLM cannot be unloaded, rendering is aborted instead of risking an OOM. Automatic detection relies on file names / directories (`sdxl`, `sd15`); use `text2img.json` when unsure.

## Configuration

### Dynamic discovery order

| Item | Order |
|---|---|
| Workspace | `DSH_WORKSPACE` → `$DSH_HOME/storages/workspace.json` (matched by `DSH_SESSION_ID`) → default workspace → cwd |
| ComfyUI directory | `COMFYUI_DIR` → common install paths (`D:\Flux\comfyui\ComfyUI_windows_portable`, …) |
| ComfyUI port | `COMFYUI_PORT` (default `8188`) |
| Models | Scans `models/diffusion_models` (kontext/krea), `models/text_encoders` (clip_l/t5xxl), `models/vae`, `models/loras` (turbo/lightning) |
| Local LLM service | `STRATA_URL` / `STRATA_PORT` → `local_models.json` services (any health/unload/load URLs) → common-port scan for Strata-compatible services |
| Writable dirs | `<workspace>/flux_comfy/{input,output,temp,user}` |

### Environment variables

| Variable | Purpose |
|---|---|
| `FLUX_IMAGE_PY` | Python command (default `py`) |
| `FLUX_IMAGE_PLUGIN_DIR` | Plugin directory (when the installed copy is separated from the source folder) |
| `COMFYUI_DIR` | ComfyUI install directory |
| `COMFYUI_PORT` | ComfyUI port (default `8188`) |
| `STRATA_URL` / `STRATA_PORT` | Local LLM service URL / port |
| `FLUX_OPEN_UI` | Set to `1` to force opening the ComfyUI page on every render |

### `local_models.json` (any local LLM)

VRAM timesharing works with **any local LLM server** — declare its health/unload/load endpoints here:

```json
{
  "services": [
    {
      "name": "my-llm",
      "health": { "url": "http://127.0.0.1:8000/health", "loaded_field": "loaded" },
      "unload": { "url": "http://127.0.0.1:8000/unload", "body": {} },
      "load":   { "url": "http://127.0.0.1:8000/load",   "body": {} }
    },
    {
      "name": "strata",
      "health": {
        "url": "http://127.0.0.1:8085/health",
        "loaded_field": "loaded",
        "service_field": "service",
        "service_value": "strata"
      },
      "unload": { "url": "http://127.0.0.1:8085/v1/unload", "body": {} },
      "load":   { "url": "http://127.0.0.1:8085/v1/load",   "body": {} }
    }
  ]
}
```

- `health.loaded_field` — JSON field telling whether the model is loaded (default `loaded`);
- optional `health.service_field` / `service_value` — extra validation (e.g. `"service": "strata"`);
- `unload` / `load` — any POST endpoint; a `busy` status or HTTP 409/503 is retried;
- without this file (or when nothing responds), the plugin falls back to Strata on `127.0.0.1:8085`.

ComfyUI itself is intentionally **not** listed here — it is managed by `gen.py`.

## Repository layout

```
.
├── gen.py                         # renderer: detection + VRAM timesharing + ComfyUI calls
├── local_models.json              # local LLM service list (health / unload / load)
├── reload_strata.py               # plugin-triggered background reload of the local LLM
├── flux_dev_fp8_api.json          # text-to-image workflow (ComfyUI API format)
├── flux_kontext_gguf_api.json     # Kontext reference workflow (ComfyUI API format)
├── sdxl_api.json                  # SDXL / SD1.5 checkpoint workflow (ComfyUI API format)
├── text2img.json.example          # example config for model/workflow override
├── README.md                      # English docs
├── README.zh-CN.md                # Chinese docs
├── LICENSE                        # MIT
└── flux-image-plugin/             # DSH plugin package
    ├── package.json
    ├── cordis.patch.yml
    └── dsh/
        ├── index.js               # registers the generate_image tool
        └── spawnHidden.js         # windowless spawn helper (Windows)
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ComfyUI 启动超时` / `Cannot connect to ComfyUI` | Check `COMFYUI_DIR`; make sure `python_embeded\python.exe` exists |
| HTTP 400 `prompt_outputs_failed_validation` | The workflow references a model that is not in ComfyUI `models/` — check the model files |
| Local LLM stays unloaded after rendering | The plugin-triggered reload failed; check that `reload_strata.py` can run, or call `POST http://127.0.0.1:8085/v1/load` manually |
| Browser page opens too often | It opens at most once per 20 minutes and only when no ComfyUI browser window is detected; set `FLUX_OPEN_UI=1` to force it |
| No console window visible | The ComfyUI console window appears per render and closes when rendering ends |

## Disclaimer

This project is **not affiliated with, endorsed by, or sponsored by** DeepSeek, Black Forest Labs, ComfyUI, Alibaba or any model provider. It is an independent integration that orchestrates local tools.

- **No warranty.** The software is provided "as is", without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose and non-infringement. The authors are not liable for any claim, damages or other liability arising from the use of the software.
- **You are responsible for the content you generate** and for complying with the licenses and acceptable-use policies of the models and tools involved (including FLUX's non-commercial licenses and any local-law restrictions).
- **Local and private.** The tool runs entirely on your machine. It does not upload prompts, reference images or generated images to any server. Only the DSH agent you configure may see the generated file path.
- **Credentials.** Never commit API tokens or credentials to this repository. The `.gitignore` excludes common secrets; use short-lived tokens and revoke them after use.

## Contributing

Issues and pull requests are welcome. Please keep changes focused, test them on a real ComfyUI + DSH setup, and describe the environment you tested on.

## License

[MIT](LICENSE)
