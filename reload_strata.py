#!/usr/bin/env python3
"""
reload_strata.py — 由 DSH 插件在出图后触发：重载本地大模型（通用版）。

读取 <插件目录>/local_models.json 的 services，逐个探测 health；命中后用该服务的
load URL + 请求体重载（带重试）。没有配置时回退到 Strata 默认 8085。
环境变量 STRATA_URL / STRATA_PORT 作为最高优先候选。

文件名沿用 reload_strata.py（插件 index.js 引用此名），但支持任意本地 LLM 服务。
"""
import json
import os
import sys
import time
import urllib.request

DEFAULT_PORTS = (8085, 8080, 8086, 8000, 1234)


def log(msg):
    print(msg, flush=True)


def build_candidates(here):
    cands = []
    env_url = os.environ.get("STRATA_URL")
    env_port = os.environ.get("STRATA_PORT")
    if env_url or env_port:
        base = env_url.rstrip("/") if env_url else f"http://127.0.0.1:{env_port}"
        cands.append({"name": "strata(env)", "health": f"{base}/health",
                      "load": f"{base}/v1/load", "body": {}, "strata": True})
    try:
        with open(os.path.join(here, "local_models.json"), encoding="utf-8") as f:
            data = json.load(f)
        for svc in data.get("services") or []:
            h = (svc.get("health") or {}).get("url")
            load = (svc.get("load") or {}).get("url")
            if h and load:
                cands.append({"name": svc.get("name") or h, "health": h, "load": load,
                              "body": (svc.get("load") or {}).get("body") or {},
                              "strata": False})
    except Exception:
        pass
    for p in DEFAULT_PORTS:
        base = f"http://127.0.0.1:{p}"
        cands.append({"name": f"strata@{p}", "health": f"{base}/health",
                      "load": f"{base}/v1/load", "body": {}, "strata": True})
    return cands


def detect(here):
    for c in build_candidates(here):
        try:
            with urllib.request.urlopen(c["health"], timeout=2) as r:
                body = json.loads(r.read().decode("utf-8"))
        except Exception:
            continue
        if c["strata"]:
            if body.get("service") != "strata" or not body.get("model"):
                continue
        elif "loaded" not in body and "model" not in body:
            continue
        return c
    return None


def reload_service(c):
    payload = json.dumps(c.get("body") or {}).encode("utf-8")
    for _ in range(100):          # 最多约 5 分钟
        try:
            req = urllib.request.Request(c["load"], data=payload,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=300) as r:
                raw = r.read().decode("utf-8", "replace")
            body = json.loads(raw) if raw.strip() else {}
            if body.get("status") in ("loaded", "ok") or body.get("loaded") is True:
                log(f"[reload] {c['name']} 完成: {body.get('status')}")
                return True
        except Exception as e:
            log(f"[reload] {c['name']} 重试中: {str(e)[:120]}")
        time.sleep(3)
    return False


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    c = detect(here)
    if not c:
        log("[reload] 未找到本地模型服务，跳过")
        sys.exit(1)
    sys.exit(0 if reload_service(c) else 1)


if __name__ == "__main__":
    main()
