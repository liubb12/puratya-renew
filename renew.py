#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MWS (cloud.m-ws.cc) 自动续期脚本 (浏览器直连 API 版，完美绕过 UI 元素查找)
依赖：pip install requests DrissionPage
"""
import os
import sys
import json
import time
import urllib.error
from datetime import datetime
import requests
from notify import notify

BASE_URL = "https://cloud.m-ws.cc"
API = f"{BASE_URL}/api"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def http(path, method="GET", token=None, payload=None):
    headers = {"User-Agent": UA}
    if token:
        headers["Cookie"] = f"__Host-mrtcloud_token={token}"
    if method != "GET":
        headers["Content-Type"] = "application/json"
    try:
        resp = requests.request(
            method,
            API + path,
            headers=headers,
            json=payload,
            timeout=30,
        )
        return resp.status_code, resp.text
    except requests.RequestException as e:
        return 0, str(e)


def now_str():
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")


def collect(token):
    items = []
    for kind, path, key in [("Bot", "/bots", "bots"), ("Site", "/sites", "sites")]:
        status, body = http(path, token=token)
        if status != 200:
            print(f"[!] 获取 {path} 失败: HTTP {status} {body}")
            items.append((kind, None, f"<列表获取失败 HTTP {status}>", None))
            continue
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = []
        lst = data if isinstance(data, list) else data.get(key, [])
        for obj in lst or []:
            oid = obj.get("id")
            name = obj.get("name") or obj.get("username") or f"id:{oid}"
            timer = obj.get("timer") or {}
            rem = timer.get("remaining_hours")
            items.append((kind, oid, name, rem))
    return items


def init_browser(token):
    from DrissionPage import ChromiumPage, ChromiumOptions

    co = ChromiumOptions()
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-gpu")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument("--window-size=1280,800")
    co.set_user_agent(UA)

    page = ChromiumPage(co)
    page.get(BASE_URL)
    page.run_cdp(
        "Network.setCookie",
        name="__Host-mrtcloud_token",
        value=token,
        domain="cloud.m-ws.cc",
        path="/",
        secure=True,
        httpOnly=True,
    )
    return page


def renew_via_browser_fetch(page, kind, oid):
    """
    通过已过盾的浏览器上下文直接发起 fetch 请求调用续期接口，
    这样既能带上 Cloudflare 的 Cookie/Session 凭证，又不需要在页面上找按钮。
    """
    api_path = f"/api/bots/{oid}/renew" if kind == "Bot" else f"/api/sites/{oid}/renew"
    js_script = f"""
    async () => {{
        try {{
            const resp = await fetch('{api_path}', {{
                method: 'POST',
                headers: {{
                    'Content-Type': 'application/json'
                }}
            }});
            const text = await resp.text();
            return {{ status: resp.status, body: text }};
        } catch (e) {{
            return {{ status: 0, body: e.toString() }};
        }}
    }}
    """
    try:
        res = page.run_js(js_script)
        if res and isinstance(res, dict):
            status = res.get("status", 0)
            body = res.get("body", "")
            return status == 200, status, body
        return False, 0, "JS 执行未返回有效结果"
    except Exception as e:
        return False, 500, str(e)


def _report(level, title, content, details):
    try:
        notify(title, content, level=level, details=details)
    except Exception as e:
        print(f"::warning::通知上报失败: {e}")


def main():
    token = os.environ.get("MWS_TOKEN", "").strip()
    if not token:
        print("[✗] 环境变量 MWS_TOKEN 未设置")
        sys.exit(1)

    status, body = http("/auth/me", token=token)
    if status == 401:
        title = f"⚠️ MWS token 已失效 ({now_str()})"
        content = "请重新登录 cloud.m-ws.cc，更新 GitHub Secret MWS_TOKEN"
        print(title)
        _report("failed", title, content, None)
        sys.exit(1)
    if status != 200:
        print(f"[✗] 验证 token 异常: HTTP {status} {body}")
        sys.exit(1)

    try:
        who = json.loads(body).get("username")
    except Exception:
        who = "?"
    print(f"[✓] 登录有效: {who}")

    items = collect(token)
    if not items:
        title = f"MWS 续期报告 ({now_str()}) · 无对象"
        print(title)
        _report("success", title, "账号下没有 Bot / Site，跳过。", None)
        return

    page = None
    try:
        page = init_browser(token)
        # 访问一次主站以确保 Cloudflare 验证通过、Cookie 生效
        page.get(BASE_URL)
        time.sleep(5)
    except Exception as e:
        print(f"[!] 浏览器启动失败: {e}")

    lines = []
    failed = 0
    detail_items = []

    for kind, oid, name, rem in items:
        if oid is None:
            lines.append(f"  · {kind} {name}：列表获取失败")
            detail_items.append({"id": "unknown", "name": name, "status": "failed", "error": "列表获取失败"})
            failed += 1
            continue

        if page:
            ok, status, resp_body = renew_via_browser_fetch(page, kind, oid)
        else:
            ok, status, resp_body = False, 500, "浏览器未成功初始化"

        rem_txt = f"，续期前剩 {int(rem)}h" if rem is not None else ""
        if ok:
            lines.append(f"  · {kind} {name}：续期成功{rem_txt}")
            msg = rem_txt.strip().lstrip("，") if rem is not None else ""
            detail_items.append({"id": str(oid), "name": name, "status": "success", "message": msg})
            print(f"[✓] {kind} {name} (id:{oid}) 续期成功{rem_txt}")
        else:
            lines.append(f"  · {kind} {name}：续期失败 HTTP {status} {resp_body.strip()}")
            detail_items.append({"id": str(oid), "name": name, "status": "failed", "error": f"HTTP {status} {resp_body.strip()}"})
            failed += 1
            print(f"[✗] {kind} {name} (id:{oid}) 续期失败 HTTP {status}")

    if page:
        try:
            page.quit()
        except Exception:
            pass

    total = len(items)
    success = total - failed
    level = "success" if failed == 0 else "partial"
    status_word = "完成" if failed == 0 else "部分失败"
    title = f"MWS 续期报告 ({now_str()}) · {status_word}"
    content = "\n".join(lines)
    print("\n" + title + "\n" + content)

    _report(
        level,
        title,
        content,
        {"total": total, "success": success, "failed": failed, "details": detail_items},
    )
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
