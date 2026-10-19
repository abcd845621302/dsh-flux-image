// DeepSeek Harness (dsh) plugin: registers a `generate_image` tool that runs
// the workspace's gen.py (FLUX.1 Krea dev text-to-image via ComfyUI, plus
// FLUX.1 Kontext dev reference-image editing).
//
// gen.py owns the whole VRAM-timeshare pipeline — it reads local_models.json,
// probes each configured local LLM's health endpoint, unloads the loaded ones,
// renders the image, then reloads them. This plugin is therefore a thin
// wrapper: validate arguments, spawn `py gen.py`, and return the absolute
// image path to the model.
//
// Routing is automatic: pass `image` -> Kontext reference-image workflow;
// omit `image` -> the normal text-to-image workflow. `accelerate` switches the
// text-to-image path to the FLUX Turbo LoRA at 8 steps.
import { existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnHidden } from './spawnHidden.js';

// The plugin directory that owns gen.py and the workflow JSONs. This plugin may run
// either from its source folder (…/ImagePlugin/plugin) or from a copied install
// (…/node_modules/dsh-flux-image), so probe a few locations instead of assuming one:
// FLUX_IMAGE_PLUGIN_DIR override → parent folders of this file → source folder.
const HERE = dirname(fileURLToPath(import.meta.url));
function findPluginRoot() {
  const env = process.env.FLUX_IMAGE_PLUGIN_DIR;
  if (env && existsSync(join(env, 'gen.py'))) {
    return env;
  }
  for (const cand of [resolve(HERE, '..'), resolve(HERE, '..', '..'), HERE]) {
    if (existsSync(join(cand, 'gen.py'))) {
      return cand;
    }
  }
  return 'D:/QianWen3.8/ImagePlugin/plugin';
}
const WORKSPACE = findPluginRoot();
const GEN_PY = join(WORKSPACE, 'gen.py');
const WORKFLOW = join(WORKSPACE, 'flux_dev_fp8_api.json');
const WORKFLOW_KONTEXT = join(WORKSPACE, 'flux_kontext_gguf_api.json');

// Python launcher. gen.py uses only the stdlib, so any Python 3 works. The
// Windows `py` launcher is used by default; override via FLUX_IMAGE_PY.
const PY = process.env.FLUX_IMAGE_PY || 'py';

const RENDER_TIMEOUT_S = 1680;
const TOOL_TIMEOUT_MS = 1860 * 1000;

export const name = 'flux-image';
export const inject = ['tools'];

export function apply(ctx, _config = {}) {
  ctx.tools.register({
    name: 'generate_image',
    description:
      'Generate or edit an image with local FLUX models (via ComfyUI). ' +
      'Text-to-image uses FLUX.1 Krea dev. If you pass `image` (a reference image path) it uses ' +
      'FLUX.1 Kontext dev to edit/redraw while keeping the reference character consistent — ideal for ' +
      'turning a reference artwork into an icon. It automatically frees VRAM from any loaded local LLM, ' +
      'renders, then reloads that LLM, so it is safe to call no matter which model is answering. ' +
      'Returns the absolute path of the saved PNG.',
    parameters: {
      type: 'object',
      properties: {
        prompt: {
          type: 'string',
          description: 'The image description (FLUX works best with detailed English prompts).',
        },
        image: {
          type: 'string',
          description:
            'Optional reference image path. When set, the Kontext reference workflow is used ' +
            '(keep the character consistent, apply the prompt as an edit instruction).',
        },
        size: {
          type: 'string',
          description: 'WIDTHxHEIGHT for text-to-image, e.g. "1024x1024". Defaults to 1024x1024.',
        },
        steps: {
          type: 'number',
          description: 'Sampling steps. Defaults: text-to-image 25, accelerate 8, Kontext 20.',
        },
        accelerate: {
          type: 'boolean',
          description:
            'Text-to-image only: load the FLUX Turbo LoRA and sample in 8 steps (~2x faster, small quality loss).',
        },
        seed: {
          type: 'number',
          description: 'Optional integer seed for reproducibility.',
        },
      },
      required: ['prompt'],
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          path: { type: 'string', description: 'Absolute path of the generated PNG.' },
          size: { type: 'string' },
          steps: { type: 'number' },
          mode: { type: 'string', description: 'text2img | kontext | accelerate' },
          warning: {
            type: 'string',
            description: 'Optional warning, e.g. a local LLM failed to reload after rendering.',
          },
        },
        required: ['path'],
        additionalProperties: false,
      },
      render: (_args, value) => [
        {
          type: 'text',
          text: value.warning
            ? `图片已生成: ${value.path}\n⚠️ ${value.warning}`
            : `图片已生成: ${value.path}`,
        },
      ],
    },
    timeoutMs: TOOL_TIMEOUT_MS,
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      const prompt = typeof args?.prompt === 'string' ? args.prompt.trim() : '';
      if (!prompt) {
        throw new Error('generate_image 需要一个非空的 "prompt"。');
      }
      const size =
        typeof args?.size === 'string' && /^\d+x\d+$/i.test(args.size) ? args.size : '1024x1024';
      const steps = Number.isFinite(args?.steps) ? Math.floor(args.steps) : 0;
      const seed = Number.isFinite(args?.seed) ? Math.floor(args.seed) : undefined;
      const accelerate = args?.accelerate === true;
      const image = typeof args?.image === 'string' ? args.image.trim() : '';

      if (!existsSync(GEN_PY)) {
        throw new Error(`找不到 gen.py: ${GEN_PY}`);
      }
      if (image && !existsSync(image)) {
        throw new Error(`参考图不存在: ${image}`);
      }

      const mode = image ? 'kontext' : accelerate ? 'accelerate' : 'text2img';

      const argv = [
        GEN_PY,
        prompt,
        '--workflow', WORKFLOW,
        '--workflow-kontext', WORKFLOW_KONTEXT,
        '--size', size,
        '--timeout', String(RENDER_TIMEOUT_S),
      ];
      if (steps > 0) argv.push('--steps', String(steps));
      if (image) argv.push('--init-image', image);
      if (accelerate) argv.push('--accelerate');
      if (seed !== undefined) argv.push('--seed', String(seed));

      const { stdout, stderr, code } = await run(PY, argv, exec.signal, { cwd: WORKSPACE });

      const critical = extractCritical(stdout);

      if (code !== 0) {
        const base = (stderr || stdout).trim().slice(0, 800);
        throw new Error(
          critical
            ? `generate_image 失败(exit ${code}): ${base}\n⚠️ 本地模型重载也失败: ${critical}`
            : `generate_image 失败(exit ${code}): ${base}`,
        );
      }

      const path = stdout.trim().split(/\r?\n/).pop()?.trim() || '';
      const result = { path, size, mode };
      if (steps > 0) {
        result.steps = steps;
      }
      if (critical) {
        result.warning = critical;
      }
      return result;
    },
  });
}

function extractCritical(stdout) {
  return stdout
    .split(/\r?\n/)
    .filter((line) => line.includes('[critical]'))
    .join('\n');
}

function run(command, args, signal, extra = {}) {
  return new Promise((resolve, reject) => {
    const child = spawnHidden(command, args, {
      stdio: ['ignore', 'pipe', 'pipe'],
      signal,
      env: process.env,
      ...extra,
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => {
      stdout += chunk;
    });
    child.stderr.on('data', (chunk) => {
      stderr += chunk;
    });
    child.on('error', reject);
    child.on('close', (code) => resolve({ stdout, stderr, code }));
  });
}
