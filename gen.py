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

    def names(*subs):
        out = []
        for sub in subs:
            d = root / sub
            if d.is_dir():
                out += [f.name for f in d.iterdir() if f.is_file() and not f.name.startswith(".")]
        return out

    dm = names("diffusion_models", "unet")
    enc = names("text_encoders", "clip")
    vaes = names("vae")
    loras = names("loras")

    def pick(items, *keys):
        for k in keys:
            for n in items:
                if k in n.lower():
                    return n
        return None

    return {
        "kontext": pick(dm, "kontext"),
        "krea": pick(dm, "krea") or pick(dm, "flux1-dev", "flux1") or next((n for n in dm if "flux" in n.lower() and "kontext" not in n.lower()), None),
        "clip_l": pick(enc, "clip_l", "clip-l") or pick(enc, "clip"),
        "t5": pick(enc, "t5xxl", "t5"),
        "vae": next((n for n in vaes if n.lower() == "ae.safetensors"), None) or pick(vaes, "ae", "flux_vae", "vae"),
        "lora": pick(loras, "turbo", "lightning"),
        "all": {"diffusion_models": dm, "text_encoders": enc, "vae": vaes, "loras": loras},
    }


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


def wait_models(comfy_base, timeout=90):
    """等 ComfyUI 把模型列表扫描完（/object_info 里能看到），避免刚启动就提交导致 400。"""
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
def apply_models(wf, models, kontext=False):
    for node in wf.values():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type", "")
        ins = node.setdefault("inputs", {})
        if ct in ("UNETLoader", "UnetLoaderGGUF"):
            name = models.get("kontext") if kontext else models.get("krea")
            if name:
                ins["unet_name"] = name
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


def patch_workflow(wf, prompt, width, height, steps, seed):
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
             comfy_base, comfy_dir, dirs, models, strata, started_proc, unloaded=False):
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
    a = ap.parse_args()
    seed = a.seed if a.seed is not None else random.randint(0, 2**31 - 1)

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
            log(f"[warn] 卸载本地模型失败(继续出图): {e}")

    started_proc = ensure_comfy(comfy_dir, port, dirs)
    wait_models(comfy_base)
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
            wf = json.loads(pathlib.Path(a.workflow_kontext).read_text(encoding="utf-8"))
            if not models.get("kontext"):
                raise RuntimeError("没找到 Kontext 模型（models/diffusion_models 里无 kontext 文件）")
            wf = apply_models(wf, models, kontext=True)
            init_name = upload_reference(a.init_image, dirs)
            steps = a.steps or 20
            wf = patch_kontext(wf, a.prompt, init_name, steps, seed)
            log(f"[info] Kontext 参考图模式")
        else:
            m = re.fullmatch(r"([0-9]+)x([0-9]+)", a.size.strip().lower())
            if not m:
                ap.error(f"--size 需为 WIDTHxHEIGHT，如 1024x1024（收到 {a.size!r}）")
            w, h = int(m.group(1)), int(m.group(2))
            wf = json.loads(pathlib.Path(a.workflow).read_text(encoding="utf-8"))
            wf = apply_models(wf, models, kontext=False)
            steps = a.steps or (8 if a.accelerate else 25)
            wf = patch_workflow(wf, a.prompt, w, h, steps, seed)
            if a.accelerate:
                if not models.get("lora"):
                    log("[warn] 没找到加速 LoRA，已关闭加速模式")
                else:
                    wf = add_lora(wf, models["lora"])
                    log(f"[info] 加速模式: {models['lora']} (8 步)")

        ts = time.strftime("%Y%m%d_%H%M%S")
        out = a.out or str(pathlib.Path(workspace) / "flux_images" / f"flux_{ts}.png")
        out_path = generate(wf, a.prompt, 0, 0, steps, seed, out, a.timeout,
                            comfy_base, comfy_dir, dirs, models, strata, started_proc,
                            unloaded=unloaded)
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
