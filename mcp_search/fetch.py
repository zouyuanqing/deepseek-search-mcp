# -*- coding: utf-8 -*-
"""fetch_page：抓取 URL 并转纯文本，供 agent 验证来源。
零依赖（urllib + HTMLParser），含 SSRF 防护（拒绝私有/保留 IP）。
"""
from __future__ import annotations

import gzip
import html
import io
import json
import re
import socket
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser
from typing import Dict, List, Optional

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

MAX_REDIRECTS = 5
DEFAULT_MAX_CHARS = 20000
DEFAULT_TIMEOUT_S = 20

# HTML 内容类标签（保留文字）
_KEEP_TAGS = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "th",
              "pre", "blockquote", "br", "article", "section", "span"}
_SKIP_TAGS = {"script", "style", "noscript", "nav", "footer", "header",
              "aside", "form", "button", "iframe", "svg", "canvas", "template"}


class _TextExtractor(HTMLParser):
    """HTML -> 纯文本：跳过脚本/导航噪声，块级标签后补换行。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skip_depth = 0
        self._title: str = ""

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title" and self._skip_depth == 0:
            self._in_title = True
            self._title_parts: List[str] = []
        if self._skip_depth == 0 and tag in _KEEP_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
            self._title = "".join(getattr(self, "_title_parts", [])).strip()
        if self._skip_depth == 0 and tag in _KEEP_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        if getattr(self, "_in_title", False):
            self._title_parts.append(data)
            return
        self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t\u3000]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _is_private_host(host: str) -> bool:
    """SSRF 防护：拒绝解析到私有/保留/链路本地地址的主机。"""
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET)
    except socket.gaierror:
        return True  # 解析失败视为不可信
    for info in infos:
        ip = info[4][0]
        if _is_private_ip(ip):
            return True
    return False


def _is_private_ip(ip: str) -> bool:
    try:
        packed = socket.inet_aton(ip)
        n = int.from_bytes(packed, "big")
    except OSError:
        return True
    # 10/8, 172.16/12, 192.168/16, 127/8, 169.254/16, 0/8, 100.64/10,
    # 192.0.0/24, 198.18/15, 224/4 组播, 240/4 保留
    if 0x0A000000 <= n <= 0x0AFFFFFF:       # 10.0.0.0/8
        return True
    if 0xAC100000 <= n <= 0xAC1FFFFF:       # 172.16.0.0/12
        return True
    if 0xC0A80000 <= n <= 0xC0A8FFFF:       # 192.168.0.0/16
        return True
    if 0x7F000000 <= n <= 0x7FFFFFFF:       # 127.0.0.0/8
        return True
    if 0xA9FE0000 <= n <= 0xA9FEFFFF:       # 169.254.0.0/16
        return True
    if 0x64400000 <= n <= 0x647FFFFF:       # 100.64.0.0/10
        return True
    if 0xC0000000 <= n <= 0xC00000FF:       # 192.0.0.0/24
        return True
    if 0xC6120000 <= n <= 0xC633FFFF:       # 198.18.0.0/15
        return True
    if n >= 0xE0000000:                     # 组播+保留
        return True
    return False


def _read_limited(fp, limit: int) -> bytes:
    """分块读取响应体，上限 limit 字节。"""
    buf = io.BytesIO()
    while buf.tell() < limit:
        chunk = fp.read(min(65536, limit - buf.tell() + 1))
        if not chunk:
            break
        buf.write(chunk)
    return buf.getvalue()


def _decode_content(body: bytes, encoding: str) -> bytes:
    """按 Content-Encoding 解压。"""
    enc = (encoding or "").strip().lower()
    if enc in ("gzip", "x-gzip"):
        try:
            return gzip.decompress(body)
        except OSError:
            raise ValueError("响应内容 gzip 解码失败")
    if enc == "deflate":
        try:
            return zlib.decompress(body)
        except zlib.error:
            raise ValueError("响应内容 deflate 解码失败")
    return body


def _fetch_raw(url: str, timeout: int, budget: int) -> "tuple[bytes, str, str]":
    """GET 抓取，返回完整解码后的 (body, content_type, final_url)。读取上限 budget。"""
    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA,
                                               "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                                               "Accept-Encoding": "gzip, deflate"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status not in (200,):
                raise ValueError("HTTP %s" % r.status)
            ctype = r.headers.get("Content-Type", "")
            raw = _read_limited(r, budget)
            body = _decode_content(raw, r.headers.get("Content-Encoding", ""))
            return body, ctype, r.geturl()
    except urllib.error.HTTPError as e:
        raise ValueError("HTTP %s" % e.code)
    except urllib.error.URLError as e:
        raise ValueError("网络错误: %s" % (e.reason,))
    except TimeoutError:
        raise ValueError("网络错误: 请求超时（%ss）" % timeout)


def fetch_page(url: str, timeout: int = DEFAULT_TIMEOUT_S,
               max_chars: int = DEFAULT_MAX_CHARS,
               allow_private: bool = False) -> dict:
    """抓取网页/JSON 并提取可读文本。返回 {url, final_url, title, text, content_type, truncated}。

    allow_private=True 时跳过 SSRF 防护（仅用于本地开发/测试；工具入口默认从环境变量
    MCP_FETCH_ALLOW_PRIVATE 读取，生产保持关闭）。
    """
    if not url or not url.strip():
        raise ValueError("参数 url 不能为空")
    url = url.strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("仅支持 http/https URL")
    host = parts.hostname or ""
    if not allow_private and _is_private_host(host):
        raise ValueError("拒绝访问内网/保留地址 URL（SSRF 防护）")
    # 非 ASCII 路径/查询做 percent-encoding（中文 URL 等）
    path = urllib.parse.quote(parts.path)
    query = urllib.parse.quote(parts.query, safe="=&?%")
    url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))

    body, ctype, final_url = _fetch_raw(url, timeout, max(262144, max_chars * 8))
    mime = ctype.split(";")[0].strip().lower()

    # JSON 直接返回原文
    if mime in ("application/json",) or (mime.endswith("+json")):
        try:
            text = json.dumps(json.loads(body.decode("utf-8", "replace")),
                              ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            text = body.decode("utf-8", "replace")
        truncated = len(text) > max_chars
        text = text[:max_chars]
        return {"url": url, "final_url": final_url, "title": "", "text": text,
                "content_type": mime, "truncated": truncated}

    # 非 HTML/文本（图片/PDF/二进制）
    if "html" not in mime and "text" not in mime and mime:
        raise ValueError("不支持的内容类型: %s" % mime)

    decoded = body.decode("utf-8", "replace")
    if "html" not in mime:
        # 纯文本
        text = html.unescape(decoded).strip()
        truncated = len(text) > max_chars
        return {"url": url, "final_url": final_url, "title": "", "text": text[:max_chars],
                "content_type": mime, "truncated": truncated}

    parser = _TextExtractor()
    try:
        parser.feed(decoded)
        parser.close()
    except Exception:
        # 残缺 HTML 不致命
        pass
    title = parser._title
    text = parser.text()
    truncated = len(text) > max_chars
    text = text[:max_chars]
    if not text:
        text = "(页面无可提取的静态文本，可能为 JavaScript 动态渲染页面)"
    return {"url": url, "final_url": final_url, "title": title, "text": text,
            "content_type": mime, "truncated": truncated}
