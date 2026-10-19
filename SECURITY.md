# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | ✅ |
| older | ❌ |

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Report privately via GitHub's **"Report a vulnerability"** (Security → Advisories → Report a
vulnerability) on this repository, or contact the maintainer through GitHub.

Include as much detail as possible:

- Affected component (`gen.py`, the DSH plugin `flux-image-plugin/`, or a workflow JSON),
- Steps to reproduce,
- Impact (e.g. command execution, path traversal, data exposure),
- Suggested fix if you have one.

You can expect an acknowledgement within a few days. Please give the maintainer a reasonable
amount of time to release a fix before public disclosure.

## Scope notes for this project

`dsh-flux-image` runs **local, user-supplied tools**:

- It starts a local ComfyUI process and calls its HTTP API (`127.0.0.1:8188` by default).
- It may call a local LLM service's control endpoints (`/v1/unload`, `/v1/load`) as configured in
  `local_models.json`.
- It reads a reference image you pass as a path and copies it into the ComfyUI input directory.

### Things that are intentionally out of scope

- Vulnerabilities in ComfyUI, FLUX models, custom nodes, or the local LLM service themselves
  (report those upstream).
- Attacks that already require local code execution or filesystem access as the current user.
- Generated image content (see the disclaimer in the README).

## Credentials

Never commit API tokens, keys or credentials. The repository `.gitignore` excludes `.gh_token`,
`*.token`, and `.env`. If you believe a credential was exposed, revoke it immediately.

## 中文说明

- 安全问题请**不要**公开提 Issue，改用仓库的「Report a vulnerability」或私下联系维护者。
- 本项目只在本机运行用户提供的 ComfyUI / 本地大模型服务，攻击面主要取决于这些外部组件。
- 切勿提交任何 token/密钥；`.gitignore` 已排除常见敏感文件。
