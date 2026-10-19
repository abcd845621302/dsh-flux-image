#!/usr/bin/env python3
"""
reload_strata.py — 由 DSH 插件在出图后触发：探测本地大模型服务并 POST /v1/load。

作为独立的 Node 子进程运行（DSH 主进程常驻，不会被出图请求结束时清理），
这样图片可以立即返回，而本地大模型在后台恢复。

探测顺序：STRATA_URL → STRATA_PORT → <插件目录>/local_models.json → 常见端口（校验 service=strata）。
"""
import json
import os
import sys
import time
import urllib.request

DEFAULT_PORTS = (8085, 8080, 8086, 8000, 1234)


def log(msg):
    print(msg, flush=True)


def detect_base():
    env_url = os.environ.get("STRATA_URL")
    if env_url:
        return env_url.rstrip("/")
    env_port = os.environ.get("STRATA_PORT")
    cands = [f"http://127.0.0.1:{env_port}"] if env_port else []
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(here, "local_models.json"), encoding="utf-8") as f:
            data = json.load(f)
        for svc in data.get("services") or []:
            h = (svc.get("health") or {}).get("url")
            if h:
                cands.append(h.rsplit("/health", 1)[0])
    except Exception:
        pass
    for port in DEFAULT_PORTS:
        u = f"http://127.0.0.1:{port}"
        if u not in cands:
            cands.append(u)
    for u in cands:
        try:
            with urllib.request.urlopen(u + "/health", timeout=2) as r:
                body = json.loads(r.read().decode("utf-8"))
            if body.get("service") == "strata" and body.get("model"):
                return u
        except Exception:
            continue
    return None


def main():
    base = None
    for _ in range(30):
        base = detect_base()
        if base:
            break
        time.sleep(1)
    if not base:
        log("[reload] 未找到本地模型服务，跳过")
        sys.exit(1)
    log(f"[reload] 服务: {base}")
    for _ in range(100):          # 最多约 5 分钟
        try:
            req = urllib.request.Request(
                base + "/v1/load", data=b"{}",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=300) as r:
                body = json.loads(r.read().decode("utf-8"))
            status = body.get("status")
            if status in ("loaded", "ok") or body.get("loaded") is True:
                log(f"[reload] 完成: {status}")
                sys.exit(0)
        except Exception as e:
            log(f"[reload] 重试中: {str(e)[:120]}")
        time.sleep(3)
    log("[reload] 重载超时")
    sys.exit(1)


if __name__ == "__main__":
    main()
