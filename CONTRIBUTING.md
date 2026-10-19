# Contributing to dsh-flux-image

Thanks for your interest in improving `dsh-flux-image`! Contributions of all kinds are welcome:
bug reports, feature requests, documentation fixes and code changes.

中文贡献者也欢迎，下面有简短的[中文说明](#中文说明)。

## Before you start

- Search the [existing issues](../../issues) to avoid duplicates.
- For larger changes, open an issue first to discuss the design.
- This project orchestrates local tools (ComfyUI + FLUX models). Please test on a real
  ComfyUI + DSH setup before submitting a change.

## Development environment

| Requirement | Notes |
|---|---|
| Python 3.10+ | `gen.py` only uses the standard library |
| Node.js 18+ | `flux-image-plugin/dsh/*.js` are ESM modules |
| ComfyUI | Windows portable build with FLUX models |
| DeepSeek Harness | To test the plugin end-to-end |

### Quick checks

```bash
python -m py_compile gen.py
python -c "import json; json.load(open('flux_dev_fp8_api.json')); json.load(open('flux_kontext_gguf_api.json')); print('workflows ok')"
node --check flux-image-plugin/dsh/index.js
```

## Pull requests

1. Fork the repository and create a topic branch (`fix/...`, `feat/...`).
2. Keep changes focused and small; one logical change per PR.
3. Update `README.md` / `README.zh-CN.md` when behaviour or configuration changes.
4. Describe your test environment and how you verified the change.
5. Make sure CI (syntax checks) passes.

## Reporting bugs

Please include:

- Operating system and GPU/VRAM,
- ComfyUI version and which FLUX models are installed,
- DSH version,
- The exact `generate_image` arguments,
- The full error output (redact tokens and personal paths).

## 中文说明

- 提交前先搜索已有 Issue，避免重复；较大的改动请先开 Issue 讨论。
- 修改后请至少在真实 ComfyUI + DSH 环境中验证一次，并在 PR 中说明测试环境。
- 修改行为或配置时，同步更新中英文 README。
- 报 Bug 时请附：系统/显卡、ComfyUI 版本与模型、DSH 版本、调用参数、完整报错（隐去 token 与个人路径）。

## Code of conduct

By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
