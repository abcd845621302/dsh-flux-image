#!/usr/bin/env python3
"""
gen.py — 本地分时出图（路径/模型/服务全动态探测）。

探测顺序（每一层都可被环境变量覆盖）：
  * 工作区:  DSH_WORKSPACE → DSH_HOME/storages/workspace.json(按 DSH_SESSION_ID 匹配) → 默认工作区 → cwd
  * ComfyUI: COMFYUI_DIR → 常见安装路径 → 报错
  * 端口:    COMFYUI_PORT(默认 8188)、STRATA_PORT
  * 模型:    扫描 ComfyUI 的 models 目录，自动找 Kontext / Krea / CLIP / VAE / 加速 LoRA
  * 本地模型: STRATA_URL/STRATA_PORT → local_models.json → 常见端口(校验 service=strata)

模式：
  * 文生图（默认）:  flux_dev_fp8_api.json
  * 参考图（Kontext）: --init-image → flux_kontext_gguf_api.json
  * 加速: --accelerate → 文生图工作流插入 Turbo/Lightning LoRA，8 步

分时逻辑：
  * 探测到的本地模型若 loaded，先 unload 腾显存；
  * ComfyUI 出图 → /free → 关闭本次自动启动的 ComfyUI → 重载本地模型。
"""
import argparse
import json
import os
import pathlib
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

DEFAULT_COMFY_PORT = 8188
DEFAULT_STRATA_PORTS = [8085, 8080, 8086, 8000, 1234, 7860]
COMFY_CANDIDATES = [
    r"D:\Flux\comfyui\ComfyUI_windows_portable",
    r"C:\Flux\ComfyUI_windows_portable",
    str(pathlib.Path.home() / "ComfyUI_windows_portable"),
    str(pathlib.Path.home() / "ComfyUI"),
]


def log(msg):
    print(msg, flush=True)


def get(url, timeout=120):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def post(url, payload, timeout=120):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8")
        return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:1200]
        raise RuntimeError(f"HTTP {e.code} {url}: {detail}") from None


# ---------------------------------------------------------------- 探测
def detect_workspace():
    env = os.environ.get("DSH_WORKSPACE")
    if env and pathlib.Path(env).is_dir():
        return pathlib.Path(env)
    home = pathlib.Path(os.environ.get("DSH_HOME") or (pathlib.Path.home() / ".dsh"))
    ws_file = home / "storages" / "workspace.json"
    try:
        data = json.loads(ws_file.read_text(encoding="utf-8"))
        workspaces = (data.get("tables") or {}).get("workspaces") or {}
        sid = os.environ.get("DSH_SESSION_ID")
        if sid:
            for w in workspaces.values():
                if sid in (w.get("sessionIds") or []):
                    p = pathlib.Path(w.get("path", ""))
                    if p.is_dir():
                        return p
        did = (data.get("global") or {}).get("defaultWorkspaceId")
        if did and did in workspaces:
            p = pathlib.Path(workspaces[did].get("path", ""))
            if p.is_dir():
                return p
        for w in workspaces.values():
            p = pathlib.Path(w.get("path", ""))
            if p.is_dir():
                return p
    except Exception as e:
        log(f"[warn] 读取工作区注册表失败: {e}")
    return pathlib.Path.cwd()


def make_dirs(workspace):
    base = pathlib.Path(workspace) / "flux_comfy"
    dirs = {
        "root": str(base),
        "input": str(base / "input"),
        "output": str(base / "output"),
        "temp": str(base / "temp"),
        "user": str(base / "user"),
        "previews": str(base / "user" / "previews"),
    }
    for k, v in dirs.items():
        if k != "root":
            pathlib.Path(v).mkdir(parents=True, exist_ok=True)
    return dirs


def detect_comfy_dir():
    env = os.environ.get("COMFYUI_DIR")
    cands = ([env] if env else []) + COMFY_CANDIDATES
    for c in cands:
        if not c:
            continue
        p = pathlib.Path(c)
        if (p / "ComfyUI" / "main.py").exists() and (p / "python_embeded" / "python.exe").exists():
            return p
    raise RuntimeError(f"找不到 ComfyUI，请设置环境变量 COMFYUI_DIR。候选: {cands}")


def comfy_port():
    try:
        return int(os.environ.get("COMFYUI_PORT", str(DEFAULT_COMFY_PORT)))
    except ValueError:
        return DEFAULT_COMFY_PORT


def comfy_alive(base, timeout=3):
    try:
        get(f"{base}/system_stats", timeout=timeout)
        return True
    except Exception:
        return False


def scan_models(comfy_dir):
    root = pathlib.Path(comfy_dir) / "ComfyUI" / "models"

    MODEL_EXTS = {".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin", ".sft"}

    def names(*subs):
        out = []
        for sub in subs:
            d = root / sub
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if not f.is_file() or f.name.startswith("."):
                    continue
                if f.suffix.lower() not in MODEL_EXTS:
                    continue
                try:
                    if f.stat().st_size < 1024 * 1024:   # 跳过占位/空文件
                        continue
                except OSError:
                    continue
                out.append(f.name)
        return out

    dm = names("diffusion_models", "unet")
    ckpts = names("checkpoints")
    enc = names("text_encoders", "clip")
    vaes = names("vae")
    loras = names("loras")

    def pick(items, *keys):
        for k in keys:
            for n in items:
                if k in n.lower():
                    return n
        return None

    # 文生图候选：diffusion_models 里的 FLUX 系（flux）与 checkpoints 里的 SD 系（sdxl / sd15）
    candidates = []
    for n in dm:
        low = n.lower()
        if "kontext" in low:
            continue
        candidates.append({"name": n, "kind": "flux", "source": "diffusion_models"})
    for n in ckpts:
        low = n.lower()
        kind = "sd15" if ("sd15" in low or "sd-1.5" in low or "v1-5" in low) else "sdxl"
        candidates.append({"name": n, "kind": kind, "source": "checkpoints"})

    return {
        "kontext": pick(dm, "kontext"),
        "krea": pick(dm, "krea") or pick(dm, "flux1-dev", "flux1") or next((n for n in dm if "flux" in n.lower() and "kontext" not in n.lower()), None),
        "clip_l": pick(enc, "clip_l", "clip-l") or pick(enc, "clip"),
        "t5": pick(enc, "t5xxl", "t5"),
        "vae": next((n for n in vaes if n.lower() == "ae.safetensors"), None) or pick(vaes, "ae", "flux_vae", "vae"),
        "lora": pick(loras, "turbo", "lightning"),
        "text2img_candidates": candidates,
        "all": {"diffusion_models": dm, "checkpoints": ckpts, "text_encoders": enc, "vae": vaes, "loras": loras},
    }


def load_text2img_config(here):
    """读取 <插件目录>/text2img.json（可选）并用环境变量覆盖；返回 dict。"""
    cfg = {}
    p = pathlib.Path(here) / "text2img.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if not str(k).startswith("_")})
            else:
                log("[warn] text2img.json 不是 JSON 对象，已忽略")
        except Exception as e:
            log(f"[warn] 读取 text2img.json 失败(忽略): {e}")
    env_map = {
        "model": "FLUX_TEXT2IMG_MODEL",
        "workflow": "FLUX_TEXT2IMG_WORKFLOW",
        "steps": "FLUX_TEXT2IMG_STEPS",
        "cfg": "FLUX_TEXT2IMG_CFG",
        "sampler": "FLUX_TEXT2IMG_SAMPLER",
        "scheduler": "FLUX_TEXT2IMG_SCHEDULER",
        "size": "FLUX_TEXT2IMG_SIZE",
    }
    for key, env in env_map.items():
        val = os.environ.get(env)
        if not val:
            continue
        if key == "steps":
            try:
                cfg[key] = int(val)
            except ValueError:
                log(f"[warn] {env} 不是整数，已忽略: {val}")
        elif key == "cfg":
            try:
                cfg[key] = float(val)
            except ValueError:
                log(f"[warn] {env} 不是数字，已忽略: {val}")
        else:
            cfg[key] = val
    return cfg


def workflow_nodes(wf):
    return {node.get("class_type") for node in wf.values() if isinstance(node, dict)}


def workflow_kind(wf):
    classes = workflow_nodes(wf)
    if "CheckpointLoaderSimple" in classes:
        return "checkpoint"       # SDXL / SD1.5 共用 checkpoint 系工作流
    if classes & {"UNETLoader", "UnetLoaderGGUF"}:
        return "flux"
    return None


def pick_text2img(models, cfg, here):
    """决定文生图用哪个模型 + 工作流。返回 {model, workflow, kind, params}。"""
    candidates = models.get("text2img_candidates") or []
    if not candidates:
        avail = models.get("all") or {}
        raise RuntimeError(
            "没有可用的文生图模型：ComfyUI 的 models/diffusion_models 与 models/checkpoints 都是空的。"
            "请把模型放进对应目录。当前目录内容: "
            + json.dumps({k: len(v or []) for k, v in avail.items()}, ensure_ascii=False)
        )
    chosen = None
    wanted = cfg.get("model")
    if wanted:
        lowered = str(wanted).lower()
        chosen = next((c for c in candidates if c["name"].lower() == lowered or c["name"] == str(wanted)), None)
        if not chosen:
            log(f"[warn] 配置的模型 {wanted} 不在 ComfyUI 模型目录中，回退自动识别。")
            log("[info] 可用候选: " + json.dumps([c["name"] for c in candidates], ensure_ascii=False))
    if not chosen:
        # 默认：Krea → 任意 FLUX → 任意 SDXL → 任意 SD1.5
        krea = models.get("krea")
        if krea:
            chosen = next((c for c in candidates if c["name"] == krea), None)
            if chosen is None:
                chosen = {"name": krea, "kind": "flux", "source": "diffusion_models"}
        if not chosen:
            for kind in ("flux", "sdxl", "sd15"):
                chosen = next((c for c in candidates if c["kind"] == kind), None)
                if chosen:
                    break
    if not chosen:
        raise RuntimeError("无法确定文生图模型，请配置 text2img.json 的 model 字段。可用候选: "
                           + json.dumps(candidates, ensure_ascii=False))
    kind = chosen["kind"]
    log(f"[info] 文生图模型: {chosen['name']} ({kind}, 来自 {chosen['source']})")

    # 工作流：配置优先，否则按模型类型选默认
    wf_name = cfg.get("workflow")
    if wf_name:
        wf_path = pathlib.Path(wf_name)
        if not wf_path.is_absolute():
            wf_path = pathlib.Path(here) / wf_name
        if not wf_path.exists():
            raise RuntimeError(f"配置的工作流不存在: {wf_path}")
    else:
        default = "flux_dev_fp8_api.json" if kind == "flux" else "sdxl_api.json"
        wf_path = pathlib.Path(here) / default
        if not wf_path.exists():
            raise RuntimeError(f"缺少默认工作流: {wf_path}（可提供自定义工作流并写入 text2img.json 的 workflow）")
    try:
        wf = json.loads(wf_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"工作流不是合法 JSON: {wf_path}: {e}")
    wf_kind = workflow_kind(wf)
    if wf_kind is None:
        raise RuntimeError(f"无法识别工作流类型（缺 CheckpointLoaderSimple / UNETLoader 节点）: {wf_path}")
    if kind in ("sdxl", "sd15") and wf_kind != "checkpoint":
        raise RuntimeError(f"模型 {chosen['name']} 是 checkpoint 系，但工作流 {wf_path.name} 缺 CheckpointLoaderSimple 节点")
    if kind == "flux" and wf_kind != "flux":
        raise RuntimeError(f"模型 {chosen['name']} 是 FLUX 系，但工作流 {wf_path.name} 缺 UNETLoader/UnetLoaderGGUF 节点")
    params = {k: cfg.get(k) for k in ("steps", "cfg", "sampler", "scheduler", "size") if cfg.get(k) is not None}
    return {"model": chosen["name"], "workflow": wf_path, "kind": kind, "params": params}


def require_nodes(comfy_base, node_names, hint=""):
    """检查 ComfyUI 是否具备这些节点，缺哪个报清楚（例如 Kontext GGUF 需要 ComfyUI-GGUF）。"""
    try:
        info = json.loads(get(f"{comfy_base}/object_info", timeout=20))
    except Exception as e:
        log(f"[warn] 获取 ComfyUI 节点列表失败(跳过节点检查): {e}")
        return
    missing = [n for n in node_names if n not in info]
    if missing:
        raise RuntimeError(
            "当前 ComfyUI 缺少所需节点: " + ", ".join(missing) + ("。" + hint if hint else "")
        )


def detect_strata():
    """返回 {"base": url, "name": model} 或 None。"""
    cands = []
    env_url = os.environ.get("STRATA_URL")
    env_port = os.environ.get("STRATA_PORT")
    if env_url:
        cands.append(env_url.rstrip("/"))
    if env_port:
        cands.append(f"http://127.0.0.1:{env_port}")
    here = pathlib.Path(__file__).resolve().parent
    lm = here / "local_models.json"
    if lm.exists():
        try:
            data = json.loads(lm.read_text(encoding="utf-8"))
            for svc in data.get("services") or []:
                h = (svc.get("health") or {}).get("url")
                if h:
                    cands.append(h.rsplit("/health", 1)[0])
        except Exception as e:
            log(f"[warn] 读取 local_models.json 失败: {e}")
    for p in DEFAULT_STRATA_PORTS:
        u = f"http://127.0.0.1:{p}"
        if u not in cands:
            cands.append(u)
    for u in cands:
        try:
            body = json.loads(get(f"{u}/health", timeout=2))
            if body.get("service") == "strata" and body.get("model"):
                return {"base": u, "name": body.get("model")}
        except Exception:
            continue
    return None


# ---------------------------------------------------------------- ComfyUI
def ensure_comfy(comfy_dir, port, dirs):
    base = f"http://127.0.0.1:{port}"
    if comfy_alive(base):
        return None
    py = pathlib.Path(comfy_dir) / "python_embeded" / "python.exe"
    main = pathlib.Path(comfy_dir) / "ComfyUI" / "main.py"
    models = pathlib.Path(comfy_dir) / "ComfyUI" / "models"
    cmd = [
        str(py), "-s", str(main),
        "--windows-standalone-build", "--disable-assets",
        "--port", str(port),
        "--input-directory", dirs["input"],
        "--output-directory", dirs["output"],
        "--temp-directory", dirs["temp"],
        "--user-directory", dirs["user"],
        "--previews-directory", dirs["previews"],
        "--models-directory", str(models),
        "--disable-auto-launch",
    ]
    log("[info] ComfyUI 未运行，正在自动启动 ...")
    flags = 0
    if os.name == "nt":
        flags = 0x00000010   # CREATE_NEW_CONSOLE：显示 ComfyUI 控制台黑窗口
    proc = subprocess.Popen(
        cmd, cwd=str(pathlib.Path(comfy_dir) / "ComfyUI"),
        creationflags=flags,
    )
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        time.sleep(2)
        if comfy_alive(base):
            log("[info] ComfyUI 已就绪")
            return proc
    raise RuntimeError("ComfyUI 启动超时(>180s)")


UI_THROTTLE_S = 20 * 60   # 20 分钟内不重复自动打开浏览器
BROWSER_EXES = (
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe",
    "360se.exe", "360chrome.exe", "qqbrowser.exe", "sogouexplorer.exe", "iexplore.exe",
)


def _window_exe(hwnd, user32, kernel32):
    """返回窗口所属进程的可执行文件小写路径（失败返回空串）。"""
    try:
        from ctypes import wintypes
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(0x1000, False, pid.value)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value.lower()
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return ""
    return ""


def ui_window_open(port):
    """Windows: 枚举可见的**浏览器**窗口标题，判断里面是否已打开 ComfyUI 页面。
    只看浏览器进程，避免把 ComfyUI 自己的控制台黑窗口误认成页面。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        found = []

        def _cb(hwnd, _):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                n = user32.GetWindowTextLengthW(hwnd)
                if n <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                t = buf.value
                if not ("ComfyUI" in t or f"127.0.0.1:{port}" in t or f"localhost:{port}" in t):
                    return True
                exe = _window_exe(hwnd, user32, kernel32)
                if any(exe.endswith(b) for b in BROWSER_EXES):
                    found.append(t)
            except Exception:
                pass
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        return bool(found)
    except Exception:
        return False


def should_open_ui(dirs, port):
    """是否自动打开 ComfyUI 页面：已有页面 → 不开；20 分钟内开过 → 不开；否则开。"""
    if os.environ.get("FLUX_OPEN_UI") == "1":
        return True
    if ui_window_open(port):
        log("[info] 检测到浏览器已有 ComfyUI 页面，跳过自动打开")
        return False
    stamp = pathlib.Path(dirs["root"]) / "ui_last_open.txt"
    try:
        last = float(stamp.read_text(encoding="utf-8").strip())
        if time.time() - last < UI_THROTTLE_S:
            log("[info] 20 分钟内已自动打开过 ComfyUI 页面，跳过")
            return False
    except Exception:
        pass
    return True


def wait_models(comfy_base, timeout=90, checks=None):
    """等 ComfyUI 把模型列表扫描完（/object_info 里能看到），避免刚启动就提交导致 400。
    checks 为 [(节点名, 字段名)]；None 时使用 FLUX 默认组合。"""
    if checks is None:
        checks = [("UNETLoader", "unet_name"), ("UnetLoaderGGUF", "unet_name"),
                  ("DualCLIPLoader", "clip_name1"), ("DualCLIPLoader", "clip_name2"),
                  ("VAELoader", "vae_name"), ("LoraLoaderModelOnly", "lora_name")]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ok = True
        for node, field in checks:
            try:
                info = json.loads(get(f"{comfy_base}/object_info/{node}", timeout=5))
            except Exception:
                ok = False
                break
            req = (((info.get(node) or {}).get("input") or {}).get("required") or {})
            lst = req.get(field)
            if not lst or not lst[0]:
                ok = False
                break
        if ok:
            return True
        time.sleep(2)
    log("[warn] ComfyUI 模型列表未就绪(继续尝试)")
    return False


def free_comfy(base, timeout=60):
    try:
        post(f"{base}/free", {"unload_models": True, "free_memory": True}, timeout=timeout)
    except Exception as e:
        log(f"[warn] ComfyUI /free 失败(忽略): {e}")


# ---------------------------------------------------------------- 工作流
def apply_models(wf, models, kontext=False, text2img_model=None):
    for node in wf.values():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type", "")
        ins = node.setdefault("inputs", {})
        if ct in ("UNETLoader", "UnetLoaderGGUF"):
            name = (text2img_model or models.get("kontext")) if kontext else (text2img_model or models.get("krea"))
            if name:
                ins["unet_name"] = name
        elif ct == "CheckpointLoaderSimple":
            if text2img_model:
                ins["ckpt_name"] = text2img_model
        elif ct == "DualCLIPLoader":
            if models.get("clip_l"):
                ins["clip_name1"] = models["clip_l"]
            if models.get("t5"):
                ins["clip_name2"] = models["t5"]
        elif ct in ("CLIPLoader",):
            if models.get("clip_l"):
                ins["clip_name"] = models["clip_l"]
        elif ct == "VAELoader":
            if models.get("vae"):
                ins["vae_name"] = models["vae"]
    return wf


def patch_workflow(wf, prompt, width, height, steps, seed, params=None):
    params = params or {}
    patched = {"text": 0, "size": 0, "steps": 0, "seed": 0}
    for node in wf.values():
        if not isinstance(node, dict) or "class_type" not in node:
            continue
        ct = node.get("class_type", "")
        ins = node.setdefault("inputs", {})
        if ct in ("CLIPTextEncode", "CLIPTextEncodeFlux"):
            if patched["text"] == 0:
                ins["text"] = prompt
            patched["text"] += 1
        elif ct in ("EmptyLatentImage", "EmptySD3LatentImage"):
            ins["width"] = width
            ins["height"] = height
            patched["size"] += 1
        elif ct == "KSampler":
            ins["steps"] = steps
            ins["seed"] = seed
            if params.get("cfg") is not None:
                ins["cfg"] = float(params["cfg"])
            if params.get("sampler"):
                ins["sampler_name"] = params["sampler"]
            if params.get("scheduler"):
                ins["scheduler"] = params["scheduler"]
            patched["steps"] += 1
            patched["seed"] += 1
        elif ct == "SamplerCustom":
            ins["noise_seed"] = seed
            patched["seed"] += 1
        elif ct == "BasicScheduler":
            ins["steps"] = steps
            patched["steps"] += 1
        elif ct == "RandomNoise":
            ins["noise_seed"] = seed
            patched["seed"] += 1
    if patched["text"] == 0:
        raise RuntimeError("工作流里没找到 CLIPTextEncode 节点(prompt)")
    if patched["steps"] == 0:
        raise RuntimeError("工作流里没找到 KSampler/BasicScheduler 节点(步数)")
    return wf


def patch_kontext(wf, prompt, init_name, steps, seed):
    for node in wf.values():
        if not isinstance(node, dict) or "class_type" not in node:
            continue
        ct = node.get("class_type", "")
        ins = node.setdefault("inputs", {})
        if ct == "LoadImage":
            ins["image"] = init_name
        elif ct == "CLIPTextEncode":
            ins["text"] = prompt
        elif ct == "KSampler":
            ins["steps"] = steps
            ins["seed"] = seed
    return wf


def add_lora(wf, lora_name, strength=1.0):
    node_id = "999"
    wf[node_id] = {
        "class_type": "LoraLoaderModelOnly",
        "inputs": {"lora_name": lora_name, "strength_model": strength},
    }
    for node in wf.values():
        if isinstance(node, dict) and node.get("class_type") == "KSampler":
            src = node["inputs"].get("model")
            if src:
                wf[node_id]["inputs"]["model"] = src
                node["inputs"]["model"] = [node_id, 0]
    return wf


def upload_reference(init_image, dirs):
    src = pathlib.Path(init_image)
    if not src.exists():
        raise RuntimeError(f"参考图不存在: {init_image}")
    name = f"ref_{int(time.time())}{src.suffix or '.png'}"
    shutil.copyfile(src, pathlib.Path(dirs["input"]) / name)
    log(f"[info] 参考图已就绪: {name}")
    return name


# ---------------------------------------------------------------- 分时 + 出图
def strata_loaded(strata):
    try:
        body = json.loads(get(f"{strata['base']}/health", timeout=5))
        return bool(body.get("loaded"))
    except Exception:
        return False


def strata_action(strata, action, retries=30, wait=2.0):
    url = f"{strata['base']}/v1/{action}"
    timeout = 300 if action == "load" else 30
    for _ in range(retries):
        try:
            r = post(url, {}, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (409, 503):
                time.sleep(wait)
                continue
            raise
        if isinstance(r, dict) and r.get("status") == "busy":
            time.sleep(wait)
            continue
        return r
    raise RuntimeError(f"Strata {action} 连续 busy")


def spawn_strata_reload(base):
    """在独立后台进程里重载本地模型，不阻塞图片返回。成功启动返回 True。"""
    code = (
        "import sys,time,json,urllib.request\n"
        "base=sys.argv[1]\n"
        "for _ in range(80):\n"
        "    try:\n"
        "        req=urllib.request.Request(base+'/v1/load',data=b'{}',headers={'Content-Type':'application/json'})\n"
        "        r=urllib.request.urlopen(req,timeout=300)\n"
        "        print('reload ok',json.load(r).get('status'),flush=True)\n"
        "        sys.exit(0)\n"
        "    except Exception as e:\n"
        "        time.sleep(3)\n"
        "sys.exit(1)\n"
    )
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x08000000   # DETACHED_PROCESS | CREATE_NO_WINDOW
    try:
        subprocess.Popen(
            [sys.executable, "-c", code, base],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
        return True
    except Exception as e:
        log(f"[warn] 后台重载进程启动失败: {e}")
        return False


def generate(wf, prompt, width, height, steps, seed, out, timeout,
             comfy_base, comfy_dir, dirs, models, strata, started_proc, unloaded=False,
             no_reload=False):
    if seed is None:
        seed = random.randint(0, 2**31 - 1)
    seed = int(seed)

    try:
        pid = None
        for attempt in range(6):
            try:
                pid = post(f"{comfy_base}/prompt", {"prompt": wf}, timeout=60)["prompt_id"]
                break
            except Exception as e:
                log(f"[warn] 提交工作流失败(第 {attempt+1} 次): {str(e)[:200]}")
                if attempt == 5:
                    raise
                time.sleep(3)
        deadline = time.monotonic() + timeout
        hist = None
        while time.monotonic() < deadline:
            try:
                hist = json.loads(get(f"{comfy_base}/history/{pid}", timeout=30))
            except Exception:
                time.sleep(2)
                continue
            if pid in hist:
                break
            time.sleep(1)
        if not hist or pid not in hist:
            raise RuntimeError(f"出图超时(>{timeout}s)，ComfyUI 可能卡住")
        for node in hist[pid].get("outputs", {}).values():
            for img in node.get("images", []):
                q = urllib.parse.urlencode({"filename": img["filename"], "subfolder": img.get("subfolder", "")})
                data = get(f"{comfy_base}/view?{q}", timeout=120)
                pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
                pathlib.Path(out).write_bytes(data)
                return str(pathlib.Path(out).resolve())
        raise RuntimeError("ComfyUI 输出里没有图片")
    finally:
        try:
            free_comfy(comfy_base)
        except Exception:
            pass
        if started_proc is not None:
            try:
                log("[info] 关闭本次启动的 ComfyUI 以释放显存 ...")
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(started_proc.pid)],
                               capture_output=True, timeout=30)
                time.sleep(3)
                log("[info] ComfyUI 已关闭")
            except Exception as e:
                log(f"[warn] 关闭 ComfyUI 失败: {e}")
        if unloaded:
            if no_reload:
                log("[info] 跳过本地模型重载（由调用方负责）")
            else:
                # 同步重载：部分宿主（如 DSH 插件沙箱）会终止后台子进程，
                # 因此这里等待重载完成，保证出图后本地模型一定恢复。
                try:
                    strata_action(strata, "load")
                    log(f"[info] 已重载本地模型 {strata['name']}")
                except Exception as e:
                    log(f"[critical] 本地模型重载失败，请手动重载; 原因: {e}")


def main():
    here = pathlib.Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("prompt")
    ap.add_argument("--size", default="1024x1024")
    ap.add_argument("--steps", type=int, default=0, help="0=自动(文生图25/加速8/Kontext20)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", default=None, help="默认 <工作区>/flux_images/flux_<时间戳>.png")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--workflow", default=str(here / "flux_dev_fp8_api.json"))
    ap.add_argument("--workflow-kontext", default=str(here / "flux_kontext_gguf_api.json"))
    ap.add_argument("--init-image", default=None)
    ap.add_argument("--accelerate", action="store_true")
    ap.add_argument("--no-reload", action="store_true", help="跳过本地模型重载（由调用方负责）")
    a = ap.parse_args()
    seed = a.seed if a.seed is not None else random.randint(0, 2**31 - 1)
    cfg = load_text2img_config(here)
    if cfg:
        log("[info] 文生图配置: " + json.dumps(cfg, ensure_ascii=False))

    workspace = detect_workspace()
    log(f"[info] 工作区: {workspace}")
    dirs = make_dirs(workspace)
    comfy_dir = detect_comfy_dir()
    log(f"[info] ComfyUI: {comfy_dir}")
    port = comfy_port()
    comfy_base = f"http://127.0.0.1:{port}"
    models = scan_models(comfy_dir)
    log("[info] 模型: " + json.dumps({k: v for k, v in models.items() if k != "all"}, ensure_ascii=False))
    strata = detect_strata()
    log(f"[info] 本地模型: {strata or '未发现'}")

    # 先卸载占显存的本地模型，再启动 ComfyUI（否则 12GB 卡装不下两者）
    unloaded = False
    if strata and strata_loaded(strata):
        try:
            strata_action(strata, "unload")
            unloaded = True
            log(f"[info] 已卸载本地模型 {strata['name']}")
        except Exception as e:
            raise RuntimeError(
                f"本地模型 {strata['name']} 正在占用显存但卸载失败，已中止出图以避免显存不足(OOM)。"
                f"请手动卸载或关闭本地模型后重试。原因: {e}"
            )

    started_proc = ensure_comfy(comfy_dir, port, dirs)
    if started_proc is not None:
        # 智能识别：浏览器里已有 ComfyUI 页面 / 20 分钟内打开过 → 不再重复打开。
        # 想强制打开：设环境变量 FLUX_OPEN_UI=1。
        if should_open_ui(dirs, port):
            try:
                webbrowser.open(f"{comfy_base}/")
                (pathlib.Path(dirs["root"]) / "ui_last_open.txt").write_text(
                    str(int(time.time())), encoding="utf-8")
                log("[info] 已打开 ComfyUI 页面")
            except Exception as e:
                log(f"[warn] 打开 ComfyUI 页面失败: {e}")

    try:
        if a.init_image:
            # Kontext 模式：模型/工作流可用环境变量覆盖
            kontext_wf = os.environ.get("FLUX_KONTEXT_WORKFLOW") or a.workflow_kontext
            kontext_model = os.environ.get("FLUX_KONTEXT_MODEL") or models.get("kontext")
            if not kontext_model:
                raise RuntimeError("没找到 Kontext 模型（models/diffusion_models 里无 kontext 文件）")
            if not pathlib.Path(kontext_wf).exists():
                raise RuntimeError(f"Kontext 工作流不存在: {kontext_wf}")
            wf = json.loads(pathlib.Path(kontext_wf).read_text(encoding="utf-8"))
            wf = apply_models(wf, models, kontext=True, text2img_model=kontext_model)
            required = ["FluxKontextImageScale", "ReferenceLatent"]
            if str(kontext_model).lower().endswith(".gguf"):
                required.append("UnetLoaderGGUF")
            require_nodes(comfy_base, required,
                          hint="Kontext GGUF 模型需要安装 ComfyUI-GGUF 自定义节点。")
            if str(kontext_model).lower().endswith(".gguf"):
                wait_models(comfy_base, checks=[("UnetLoaderGGUF", "unet_name")])
            init_name = upload_reference(a.init_image, dirs)
            steps = a.steps or 20
            wf = patch_kontext(wf, a.prompt, init_name, steps, seed)
            log("[info] Kontext 参考图模式")
        else:
            picked = pick_text2img(models, cfg, here)
            size_str = a.size if a.size != "1024x1024" else (picked["params"].get("size") or a.size)
            m = re.fullmatch(r"([0-9]+)x([0-9]+)", str(size_str).strip().lower())
            if not m:
                ap.error(f"--size 需为 WIDTHxHEIGHT，如 1024x1024（收到 {size_str!r}）")
            w, h = int(m.group(1)), int(m.group(2))
            wf = json.loads(picked["workflow"].read_text(encoding="utf-8"))
            wf = apply_models(wf, models, kontext=False, text2img_model=picked["model"])
            if picked["kind"] == "flux":
                require_nodes(comfy_base, ["UNETLoader"])
                wait_models(comfy_base, checks=[
                    ("UNETLoader", "unet_name"), ("DualCLIPLoader", "clip_name1"),
                    ("VAELoader", "vae_name"),
                ])
            else:
                require_nodes(comfy_base, ["CheckpointLoaderSimple"])
                wait_models(comfy_base, checks=[("CheckpointLoaderSimple", "ckpt_name")])
            steps = a.steps or picked["params"].get("steps") or (8 if a.accelerate else 25)
            steps = int(steps)
            params = dict(picked["params"])
            if a.accelerate:
                if picked["kind"] != "flux":
                    log("[warn] 加速 LoRA 仅适用于 FLUX 模型，当前模型已忽略 accelerate")
                elif not models.get("lora"):
                    log("[warn] 没找到加速 LoRA，已关闭加速模式")
                else:
                    wf = add_lora(wf, models["lora"])
                    params.setdefault("cfg", 1.0)
                    log(f"[info] 加速模式: {models['lora']} ({steps} 步)")
            wf = patch_workflow(wf, a.prompt, w, h, steps, seed, params)

        ts = time.strftime("%Y%m%d_%H%M%S")
        out = a.out or str(pathlib.Path(workspace) / "flux_images" / f"flux_{ts}.png")
        out_path = generate(wf, a.prompt, 0, 0, steps, seed, out, a.timeout,
                            comfy_base, comfy_dir, dirs, models, strata, started_proc,
                            unloaded=unloaded, no_reload=a.no_reload)
    finally:
        if started_proc is not None and started_proc.poll() is None:
            # 出图中途异常时也关闭启动的 ComfyUI
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(started_proc.pid)],
                               capture_output=True, timeout=30)
            except Exception:
                pass
    print(out_path)


if __name__ == "__main__":
    main()
