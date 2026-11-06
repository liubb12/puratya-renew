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


def safe_click(ele):
    """安全点击元素：优先模拟鼠标真实点击，若无位置大小则自动回退为 JS 点击"""
    if not ele:
        return False
    try:
        ele.click(by_js=False)
        return True
    except Exception:
        try:
            ele.click(by_js=True)
            return True
        except Exception as e:
            print(f"[!] 点击元素异常: {e}")
            return False


def init_browser(token):
    """初始化 DrissionPage 浏览器并注入登录 Cookie"""
    from DrissionPage import ChromiumPage, ChromiumOptions

    co = ChromiumOptions()
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-gpu")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--disable-blink-features=AutomationControlled")
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


def find_renew_btn_for_name(page, name):
    """通过 JS 在页面中精准查找对应实例名称卡片内的「延長」按钮"""
    js_code = """
    const targetName = arguments[0];
    const buttons = Array.from(document.querySelectorAll('button'));
    const
