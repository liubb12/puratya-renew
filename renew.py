#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MWS (cloud.m-ws.cc) 自动续期脚本 (支持 Cloudflare Turnstile 自动过盾)
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
    """拉取所有 Bot / Site，返回 [(kind, id, name, remaining_hours), ...]。"""
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
    """初始化 DrissionPage 浏览器并注入登录 Cookie"""
    from DrissionPage import ChromiumPage, ChromiumOptions

    co = ChromiumOptions()
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-gpu")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--window-size=1280,800")
    co.set_user_agent(UA)

    page = ChromiumPage(co)
    # 先访问一次主站以设置安全 Cookie (__Host- 前缀要求 secure + path=/)
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


def renew_via_browser(page, token, kind, oid, name):
    """
    通过真实浏览器页面点击「延長」按钮并自动完成 Cloudflare Turnstile 验证。
    返回 (ok, status_code, message)
    """
    # 记录续期前的剩余时间
    before_items = {
        (k, i): r for k, i, _, r in collect(token) if i is not None
    }
    before_rem = before_items.get((kind, oid))

    # 打开对应面板页面并监听续期 API 包
    api_target = f"/api/bots/{oid}/renew" if kind == "Bot" else f"/api/sites/{oid}/renew"
    page.listen.start(api_target)

    # 访问控制台页面
    page.get(BASE_URL)
    time.sleep(3)

    # 如果页面有左侧菜单切换 (Bot / Web)
    if kind == "Site":
        web_tab = page.ele("text:Web", timeout=5)
        if web_tab:
            web_tab.click()
            time.sleep(2)
    else:
        bot_tab = page.ele("text:Bot", timeout=5)
        if bot_tab:
            bot_tab.click()
            time.sleep(2)

    # 找到包含该实例名称的卡片，并点击里面的「延長」按钮
    card = page.ele(f"xpath://*[contains(text(), '{name}')]/ancestor::div[.//button[contains(., '延長')]][1]", timeout=8)
    if card:
        renew_btn = card.ele("xpath:.//button[contains(., '延長')]", timeout=5)
    else:
        renew_btn = page.ele("xpath://button[contains(., '延長')]", timeout=5)

    if not renew_btn:
        page.listen.stop()
        return False, 0, "页面未找到「延長」按钮"

    renew_btn.click()
    print(f"[*] 已点击 {name} 的「延長」按钮，等待 Cloudflare Turnstile 验证...")

    # 等待弹窗里的 Turnstile 自动验证或手动点击勾选框 (最多等 25 秒)
    start_t = time.time()
    while time.time() - start_t < 25:
        # 检查是否已经发出了带 token 的 renew 请求
        pkt = page.listen.wait(timeout=2)
        if pkt:
            status = pkt.response.status if pkt.response else 0
            body = pkt.response.raw_body if pkt.response else ""
            page.listen.stop()
            if status == 200:
                return True, 200, str(body)
            # 如果第一次点击触发的是 403 弹窗拦截，继续等验证完成后的二次请求
            if status != 403:
                return False, status, str(body)

        # 尝试定位 Turnstile iframe 并点击复选框（如果未自动通过）
        try:
            cf_iframe = page.get_frame("xpath://iframe[contains(@src, 'challenges.cloudflare.com')]", timeout=1)
            if cf_iframe:
                cb = cf_iframe.ele("css:input[type='checkbox']", timeout=1) or cf_iframe.ele("css:body", timeout=1)
                if cb:
                    cb.click()
        except Exception:
            pass

    page.listen.stop()

    # 兜底检查：重新拉取一次 API 看看 remaining_hours 是否已经重置变大
    after_items = {
        (k, i): r for k, i, _, r in collect(token) if i is not None
    }
    after_rem = after_items.get((kind, oid))
    if before_rem is not None and after_rem is not None and after_rem > before_rem:
        return True, 200, "通过时长比对确认续期成功"

    return False, 403, "Cloudflare Turnstile 验证超时或未通过"


def _report(level, title, content, details):
    try:
        notify(title, content, level=level, details=details)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print(f"::warning::通知上报失败 HTTP {e.code}: {body}")
    except Exception as e:
        print(f"::warning::通知上报失败: {e}")


def main():
    token = os.environ.get("MWS_TOKEN", "").strip()
    if not token:
        print("[✗] 环境变量 MWS_TOKEN 未设置")
        sys.exit(1)

    # 0) 验证 token 有效性
    status, body = http("/auth/me", token=token)
    if status == 401:
        title = f"⚠️ MWS token 已失效 ({now_str()})"
        content = (
            "请重新登录 cloud.m-ws.cc，F12 抓取 __Host-mrtcloud_token，"
            "更新到 GitHub Secret MWS_TOKEN"
        )
        print(title)
        print(content)
        _report("failed", title, content, None)
        sys.exit(1)
    if status != 200:
        print(f"[✗] 验证 token 异常: HTTP {status} {body}")
        sys.exit(1)

    try:
        who = json.loads(body).get("username")
    except (json.JSONDecodeError, AttributeError):
        who = "?"
    print(f"[✓] 登录有效: {who}")

    # 1) 拉取列表
    items = collect(token)
    if not items:
        title = f"MWS 续期报告 ({now_str()}) · 无对象"
        print(title)
        _report("success", title, "账号下没有 Bot / Site，跳过。", None)
        return

    # 初始化浏览器用于过 Turnstile 盾
    page = None
    try:
        page = init_browser(token)
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
            ok, status, body = renew_via_browser(page, token, kind, oid, name)
        else:
            ok, status, body = False, 500, "浏览器未成功初始化"

        rem_txt = f"，续期前剩 {int(rem)}h" if rem is not None else ""
        if ok:
            lines.append(f"  · {kind} {name}：续期成功{rem_txt}")
            msg = rem_txt.strip().lstrip("，") if rem is not None else ""
            detail_items.append({"id": str(oid), "name": name, "status": "success", "message": msg})
            print(f"[✓] {kind} {name} (id:{oid}) 续期成功{rem_txt}")
        else:
            lines.append(f"  · {kind} {name}：续期失败 HTTP {status} {body.strip()}")
            detail_items.append({"id": str(oid), "name": name, "status": "failed", "error": f"HTTP {status} {body.strip()}"})
            failed += 1
            print(f"[✗] {kind} {name} (id:{oid}) 续期失败 HTTP {status} {body.strip()}")

    if page:
        try:
            page.quit()
        except Exception:
            pass

    # 2) 汇总 + 通知
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
