# -*- coding: utf-8 -*-

# 网页离线化插件：下载 html 正文，并把 css / js / 图片 / 字体 / 媒体等资源一并落到本地，
# 同时把 html 里的引用改写成本地相对路径，产出一份可脱网打开的静态副本。
import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from logging import Logger
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

from engine import PluginCore
from model import Meta, PluginInvokeResponse, PluginContext
from util import FileSystem

DEFAULT_USER_AGENT = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36')

# 资源类型 -> 兜底扩展名，下载回来的响应没有可用后缀时使用
FALLBACK_EXT = {
    'css': '.css',
    'js': '.js',
    'img': '.img',
    'font': '.font',
    'media': '.media',
    'other': '.bin',
}

CONTENT_TYPE_EXT = {
    'text/css': '.css',
    'text/javascript': '.js',
    'application/javascript': '.js',
    'application/x-javascript': '.js',
    'image/png': '.png',
    'image/jpeg': '.jpg',
    'image/gif': '.gif',
    'image/webp': '.webp',
    'image/avif': '.avif',
    'image/svg+xml': '.svg',
    'image/x-icon': '.ico',
    'image/vnd.microsoft.icon': '.ico',
    'font/woff': '.woff',
    'font/woff2': '.woff2',
    'font/ttf': '.ttf',
    'font/otf': '.otf',
    'application/font-woff': '.woff',
    'application/font-woff2': '.woff2',
    'video/mp4': '.mp4',
    'audio/mpeg': '.mp3',
}

# html 中承载资源地址的属性，(标签, 属性, 资源类型)
HTML_ASSET_ATTRS = [
    ('script', 'src', 'js'),
    ('img', 'src', 'img'),
    ('img', 'data-src', 'img'),
    ('image', 'href', 'img'),
    ('source', 'src', 'media'),
    ('video', 'src', 'media'),
    ('video', 'poster', 'img'),
    ('audio', 'src', 'media'),
    ('embed', 'src', 'media'),
    ('object', 'data', 'media'),
]

# link 标签按 rel 判定资源类型，rel 可能是 "shortcut icon" 这种复合值，按词逐个匹配
LINK_REL_TYPE = {
    'stylesheet': 'css',
    'icon': 'img',
    'apple-touch-icon': 'img',
    'apple-touch-icon-precomposed': 'img',
    'mask-icon': 'img',
    'modulepreload': 'js',
    'preload': 'other',
    'manifest': 'other',
}

CSS_URL_PATTERN = re.compile(r'url\(\s*([\'"]?)(?!data:|about:)([^\'")]+)\1\s*\)', re.IGNORECASE)
CSS_IMPORT_PATTERN = re.compile(r'@import\s+([\'"])(?!data:)([^\'"]+)\1', re.IGNORECASE)


class HttpRequestPlugin(PluginCore):
    """
    配置项（context.get_config()）：
        url                 : 必填，目标页面地址
        out_dir             : 必填，输出根目录，页面副本落在 out_dir/<page_dir>/
        page_dir            : 可选，页面子目录名，默认按 url 生成
        render              : 'auto'(默认) / True / False，是否用无头浏览器渲染 js
        render_wait_until   : playwright 等待条件，默认 'networkidle'
        render_wait_selector: 渲染后额外等待出现的选择器，可选
        render_extra_wait   : 渲染完成后再静置的秒数，默认 1.0
        min_text_length     : render='auto' 时判定"静态页是空壳"的正文字符阈值，默认 200
        timeout             : 单次请求超时秒数，默认 30
        retry               : 单个资源下载重试次数，默认 2
        headers             : 附加请求头 dict
        cookies             : 附加 cookie dict
        download_assets     : 是否下载资源，默认 True
        asset_types         : 需要下载的资源类型，默认 ['css','js','img','font','media','other']
        same_origin_only    : 只下载同源资源，默认 False
        max_assets          : 资源数量上限，默认 300
        css_depth           : css 内 url()/@import 的递归下载层数，默认 2
        concurrency         : 下载并发数，默认 8
        rewrite_links       : 是否把 html/css 内引用改写成本地路径，默认 True
    """

    def __init__(self, logger: Logger, meta: Meta) -> None:
        super().__init__(logger, meta)
        self._session: Optional[requests.Session] = None
        self._conf: Dict = {}
        self._assets_dir = ''
        self._page_dir = ''
        # 原始 url -> 本地文件名（相对 assets 目录）
        self._url_to_local: Dict[str, str] = {}
        # 已占用的本地文件名，避免同名覆盖
        self._used_names = set()
        self._failed: List[Dict] = []

    def invoke(self, context: PluginContext) -> PluginInvokeResponse:
        super(HttpRequestPlugin, self).invoke(context)
        config_data = context.get_config()

        url = config_data.get('url')
        if not url:
            return PluginInvokeResponse.create_failed_response(-1, 'config error: url is empty')
        out_dir = config_data.get('out_dir')
        if not out_dir:
            return PluginInvokeResponse.create_failed_response(-1, 'config error: out_dir is empty')

        self._conf = config_data
        self._session = self.__create_session()

        page_dir_name = config_data.get('page_dir') or self.__build_page_dir_name(url)
        self._page_dir = os.path.join(out_dir, page_dir_name)
        self._assets_dir = os.path.join(self._page_dir, 'assets')
        FileSystem.create_folder(self._page_dir)
        FileSystem.create_folder(self._assets_dir)

        try:
            html, final_url, render_used = self.__fetch_html(url)
        except Exception as e:
            self._logger.error(f'fetch html failed: {e}')
            return PluginInvokeResponse.create_failed_response(-1, f'fetch html failed: {e}')

        if not html:
            return PluginInvokeResponse.create_failed_response(-1, 'fetch html failed: empty content')

        raw_html_path = os.path.join(self._page_dir, 'raw.html')
        self.__write_text(raw_html_path, html)
        self._logger.info(f'html downloaded: {len(html)} chars -> {raw_html_path} (render={render_used})')

        soup = BeautifulSoup(html, 'lxml')
        base_url = self.__resolve_base_url(soup, final_url)

        assets: List[Dict] = []
        if config_data.get('download_assets', True):
            assets = self.__download_assets(soup, base_url)
            self._logger.info(f'assets downloaded: {len(assets)} ok, {len(self._failed)} failed')

        if config_data.get('rewrite_links', True):
            self.__rewrite_html(soup, base_url)

        html_path = os.path.join(self._page_dir, 'index.html')
        self.__write_text(html_path, str(soup))

        title = soup.title.get_text(strip=True) if soup.title else ''
        result = {
            'url': url,
            'final_url': final_url,
            'base_url': base_url,
            'title': title,
            'render_used': render_used,
            'page_dir': self._page_dir,
            'assets_dir': self._assets_dir,
            'raw_html_path': raw_html_path,
            'html_path': html_path,
            'assets': assets,
            'failed_assets': self._failed,
            'url_to_local': dict(self._url_to_local),
        }
        self.__write_text(os.path.join(self._page_dir, 'meta.json'),
                          json.dumps(result, ensure_ascii=False, indent=2))
        self._logger.info(f'page saved: {html_path}')
        return PluginInvokeResponse.create_succeed_response(result)

    # ------------------------------------------------------------------ html

    def __create_session(self) -> requests.Session:
        session = requests.Session()
        headers = {'User-Agent': self._conf.get('user_agent', DEFAULT_USER_AGENT),
                   'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'}
        headers.update(self._conf.get('headers') or {})
        session.headers.update(headers)
        cookies = self._conf.get('cookies') or {}
        for name, value in cookies.items():
            session.cookies.set(name, value)
        return session

    def __fetch_html(self, url: str) -> Tuple[str, str, bool]:
        """
        :return: (html, 最终地址, 是否走了无头浏览器渲染)
        """
        render = self._conf.get('render', 'auto')
        if render is True:
            return self.__fetch_by_browser(url) + (True,)

        response = self._session.get(url, timeout=self._conf.get('timeout', 30))
        response.raise_for_status()
        if not response.encoding or response.encoding.lower() == 'iso-8859-1':
            response.encoding = response.apparent_encoding
        html, final_url = response.text, response.url

        if render is False:
            return html, final_url, False

        # auto：静态结果正文过短说明是前端渲染的空壳，退回浏览器渲染
        text_length = len(BeautifulSoup(html, 'lxml').get_text(strip=True))
        min_text_length = self._conf.get('min_text_length', 200)
        if text_length >= min_text_length:
            self._logger.debug(f'static html has {text_length} chars text, skip browser render')
            return html, final_url, False

        self._logger.info(f'static html only has {text_length} chars text, fallback to browser render')
        try:
            html, final_url = self.__fetch_by_browser(url)
            return html, final_url, True
        except Exception as e:
            self._logger.warning(f'browser render failed, use static html instead: {e}')
            return html, final_url, False

    def __fetch_by_browser(self, url: str) -> Tuple[str, str]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError('playwright is required for render mode, '
                               'install by: pip install playwright && playwright install chromium') from e

        timeout_ms = int(self._conf.get('timeout', 30)) * 1000
        wait_until = self._conf.get('render_wait_until', 'networkidle')
        wait_selector = self._conf.get('render_wait_selector')
        extra_wait = float(self._conf.get('render_extra_wait', 1.0))

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                context = browser.new_context(
                    user_agent=self._conf.get('user_agent', DEFAULT_USER_AGENT),
                    extra_http_headers=self._conf.get('headers') or {},
                )
                page = context.new_page()
                self._logger.info(f'render by headless chromium: {url}')
                page.goto(url, wait_until=wait_until, timeout=timeout_ms)
                if wait_selector:
                    page.wait_for_selector(wait_selector, timeout=timeout_ms)
                if extra_wait > 0:
                    time.sleep(extra_wait)
                return page.content(), page.url
            finally:
                browser.close()

    @staticmethod
    def __resolve_base_url(soup: BeautifulSoup, final_url: str) -> str:
        base_tag = soup.find('base', href=True)
        if base_tag:
            return urljoin(final_url, base_tag['href'])
        return final_url

    # ---------------------------------------------------------------- assets

    def __collect_html_assets(self, soup: BeautifulSoup, base_url: str) -> List[Tuple[str, str]]:
        """扫描 dom，收集 (绝对地址, 资源类型) 列表"""
        found: List[Tuple[str, str]] = []

        for tag_name, attr, asset_type in HTML_ASSET_ATTRS:
            for tag in soup.find_all(tag_name):
                value = tag.get(attr)
                if value:
                    found.append((urljoin(base_url, value.strip()), asset_type))

        for tag in soup.find_all('link', href=True):
            asset_type = None
            for rel in (tag.get('rel') or []):
                asset_type = LINK_REL_TYPE.get(rel.strip().lower())
                if asset_type:
                    break
            # canonical / alternate(hreflang) / preconnect 等指向的不是资源，跳过
            if asset_type is None:
                continue
            asset_url = urljoin(base_url, tag['href'].strip())
            if asset_type == 'other':
                asset_type = self.__guess_type_by_as(tag.get('as'))
            if asset_type == 'other':
                asset_type = self.__guess_type_by_url(asset_url)
            found.append((asset_url, asset_type))

        # srcset 形如 "a.png 1x, b.png 2x"
        for tag in soup.find_all(['img', 'source']):
            for attr in ('srcset', 'data-srcset'):
                srcset = tag.get(attr)
                if not srcset:
                    continue
                for candidate in srcset.split(','):
                    parts = candidate.strip().split()
                    if parts:
                        found.append((urljoin(base_url, parts[0]), 'img'))

        # 行内 style 里的 url()
        for tag in soup.find_all(style=True):
            for asset_url in self.__extract_css_urls(tag['style'], base_url):
                found.append((asset_url, 'img'))

        # <style> 块里的 url() / @import
        for tag in soup.find_all('style'):
            for asset_url in self.__extract_css_urls(tag.get_text(), base_url):
                found.append((asset_url, 'other'))

        return found

    @staticmethod
    def __guess_type_by_as(as_value: Optional[str]) -> str:
        mapping = {'style': 'css', 'script': 'js', 'image': 'img',
                   'font': 'font', 'video': 'media', 'audio': 'media'}
        return mapping.get((as_value or '').lower(), 'other')

    @staticmethod
    def __extract_css_urls(css_text: str, base_url: str) -> List[str]:
        urls = []
        for match in CSS_URL_PATTERN.finditer(css_text or ''):
            urls.append(urljoin(base_url, match.group(2).strip()))
        for match in CSS_IMPORT_PATTERN.finditer(css_text or ''):
            urls.append(urljoin(base_url, match.group(2).strip()))
        return urls

    def __download_assets(self, soup: BeautifulSoup, base_url: str) -> List[Dict]:
        wanted_types = set(self._conf.get('asset_types')
                           or ['css', 'js', 'img', 'font', 'media', 'other'])
        max_assets = int(self._conf.get('max_assets', 300))
        origin = urlparse(base_url).netloc

        pending: List[Tuple[str, str]] = []
        seen = set()
        for asset_url, asset_type in self.__collect_html_assets(soup, base_url):
            if not self.__is_downloadable(asset_url):
                continue
            if asset_type not in wanted_types:
                continue
            if self._conf.get('same_origin_only', False) and urlparse(asset_url).netloc != origin:
                continue
            if asset_url in seen:
                continue
            seen.add(asset_url)
            pending.append((asset_url, asset_type))

        downloaded: List[Dict] = []
        css_depth = int(self._conf.get('css_depth', 2))
        concurrency = int(self._conf.get('concurrency', 8))

        for depth in range(css_depth + 1):
            if not pending:
                break
            room = max_assets - len(downloaded)
            if room <= 0:
                self._logger.warning(f'reach max_assets={max_assets}, {len(pending)} assets skipped')
                break
            batch, pending = pending[:room], pending[room:]

            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                results = list(pool.map(lambda item: self.__download_one(item[0], item[1]), batch))

            next_round: List[Tuple[str, str]] = []
            for item in results:
                if item is None:
                    continue
                downloaded.append(item)
                # css 内部还挂着字体和背景图，继续往下捞一层
                if item['type'] == 'css' and depth < css_depth:
                    for sub_url in self.__extract_css_urls(self.__read_text(item['local_path']), item['url']):
                        if sub_url in seen or not self.__is_downloadable(sub_url):
                            continue
                        if self._conf.get('same_origin_only', False) and urlparse(sub_url).netloc != origin:
                            continue
                        sub_type = self.__guess_type_by_url(sub_url)
                        if sub_type not in wanted_types:
                            continue
                        seen.add(sub_url)
                        next_round.append((sub_url, sub_type))
            pending = next_round + pending

        if self._conf.get('rewrite_links', True):
            self.__rewrite_css_files(downloaded)
        return downloaded

    @staticmethod
    def __is_downloadable(asset_url: str) -> bool:
        return urlparse(asset_url).scheme in ('http', 'https')

    @staticmethod
    def __guess_type_by_url(asset_url: str) -> str:
        ext = os.path.splitext(urlparse(asset_url).path)[1].lower()
        if ext in ('.css',):
            return 'css'
        if ext in ('.js', '.mjs'):
            return 'js'
        if ext in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.avif', '.svg', '.ico', '.bmp'):
            return 'img'
        if ext in ('.woff', '.woff2', '.ttf', '.otf', '.eot'):
            return 'font'
        if ext in ('.mp4', '.webm', '.mp3', '.ogg', '.wav', '.m4a'):
            return 'media'
        return 'other'

    def __download_one(self, asset_url: str, asset_type: str) -> Optional[Dict]:
        retry = int(self._conf.get('retry', 2))
        timeout = self._conf.get('timeout', 30)
        last_error = ''
        for attempt in range(retry + 1):
            try:
                response = self._session.get(asset_url, timeout=timeout, stream=True)
                response.raise_for_status()
                content = response.content
                content_type = (response.headers.get('Content-Type') or '').split(';')[0].strip().lower()
                file_name = self.__build_asset_name(asset_url, asset_type, content_type)
                local_path = os.path.join(self._assets_dir, file_name)
                with open(local_path, 'wb') as file:
                    file.write(content)
                self._url_to_local[asset_url] = file_name
                self._logger.debug(f'asset ok [{asset_type}] {asset_url} -> assets/{file_name}')
                return {
                    'url': asset_url,
                    'type': asset_type,
                    'content_type': content_type,
                    'size': len(content),
                    'local_name': file_name,
                    'local_path': local_path,
                    'relative_path': f'assets/{file_name}',
                }
            except Exception as e:
                last_error = str(e)
                if attempt < retry:
                    time.sleep(0.5 * (attempt + 1))
        self._logger.warning(f'asset failed {asset_url}: {last_error}')
        self._failed.append({'url': asset_url, 'type': asset_type, 'error': last_error})
        return None

    def __build_asset_name(self, asset_url: str, asset_type: str, content_type: str) -> str:
        """本地文件名 = 可读名 + url 短哈希 + 扩展名，既好认又不会撞车"""
        path = urlparse(asset_url).path
        raw_name = unquote(os.path.basename(path))
        stem, ext = os.path.splitext(raw_name)
        stem = re.sub(r'[^A-Za-z0-9_.-]+', '_', stem).strip('._')[:60] or 'asset'
        if not ext or len(ext) > 8:
            ext = CONTENT_TYPE_EXT.get(content_type, FALLBACK_EXT.get(asset_type, '.bin'))
        ext = re.sub(r'[^A-Za-z0-9.]+', '', ext)

        digest = hashlib.md5(asset_url.encode('utf-8')).hexdigest()[:8]
        file_name = f'{stem}-{digest}{ext}'
        while file_name in self._used_names:
            digest = hashlib.md5((asset_url + file_name).encode('utf-8')).hexdigest()[:8]
            file_name = f'{stem}-{digest}{ext}'
        self._used_names.add(file_name)
        return file_name

    # --------------------------------------------------------------- rewrite

    def __local_ref(self, absolute_url: str) -> Optional[str]:
        file_name = self._url_to_local.get(absolute_url)
        return f'assets/{file_name}' if file_name else None

    def __rewrite_html(self, soup: BeautifulSoup, base_url: str) -> None:
        """把 dom 上的远端地址替换成本地相对路径，没下载成功的补成绝对地址保证仍可访问"""

        def replace(tag, attr, keep_absolute=True):
            value = tag.get(attr)
            if not value or value.strip().startswith(('data:', 'about:', '#')):
                return
            absolute_url = urljoin(base_url, value.strip())
            local = self.__local_ref(absolute_url)
            if local:
                tag[attr] = local
            elif keep_absolute:
                tag[attr] = absolute_url

        for tag_name, attr, _ in HTML_ASSET_ATTRS:
            for tag in soup.find_all(tag_name):
                replace(tag, attr)
        for tag in soup.find_all('link', href=True):
            replace(tag, 'href')
        for tag in soup.find_all('a', href=True):
            # 超链接不下载，只补成绝对地址，避免离线副本里点开变成死链
            value = tag['href'].strip()
            if not value.startswith(('#', 'javascript:', 'mailto:', 'data:')):
                tag['href'] = urljoin(base_url, value)

        for tag in soup.find_all(['img', 'source']):
            for attr in ('srcset', 'data-srcset'):
                srcset = tag.get(attr)
                if not srcset:
                    continue
                rewritten = []
                for candidate in srcset.split(','):
                    parts = candidate.strip().split()
                    if not parts:
                        continue
                    absolute_url = urljoin(base_url, parts[0])
                    parts[0] = self.__local_ref(absolute_url) or absolute_url
                    rewritten.append(' '.join(parts))
                tag[attr] = ', '.join(rewritten)

        for tag in soup.find_all(style=True):
            tag['style'] = self.__rewrite_css_text(tag['style'], base_url, 'assets/')
        for tag in soup.find_all('style'):
            if tag.string:
                tag.string.replace_with(self.__rewrite_css_text(tag.get_text(), base_url, 'assets/'))

        # <base> 会让本地相对路径重新指回远端，必须摘掉
        for tag in soup.find_all('base'):
            tag.decompose()

    def __rewrite_css_text(self, css_text: str, base_url: str, prefix: str) -> str:
        def on_url(match):
            quote, raw = match.group(1), match.group(2).strip()
            absolute_url = urljoin(base_url, raw)
            file_name = self._url_to_local.get(absolute_url)
            target = f'{prefix}{file_name}' if file_name else absolute_url
            return f'url({quote}{target}{quote})'

        def on_import(match):
            quote, raw = match.group(1), match.group(2).strip()
            absolute_url = urljoin(base_url, raw)
            file_name = self._url_to_local.get(absolute_url)
            target = f'{prefix}{file_name}' if file_name else absolute_url
            return f'@import {quote}{target}{quote}'

        css_text = CSS_URL_PATTERN.sub(on_url, css_text or '')
        return CSS_IMPORT_PATTERN.sub(on_import, css_text)

    def __rewrite_css_files(self, downloaded: List[Dict]) -> None:
        """css 文件和它引用的字体、图片都躺在 assets/ 下，改成同目录引用即可"""
        for item in downloaded:
            if item['type'] != 'css':
                continue
            css_text = self.__read_text(item['local_path'])
            if not css_text:
                continue
            self.__write_text(item['local_path'], self.__rewrite_css_text(css_text, item['url'], ''))

    # ----------------------------------------------------------------- utils

    @staticmethod
    def __build_page_dir_name(url: str) -> str:
        parsed = urlparse(url)
        slug = f"{parsed.netloc}{parsed.path}".strip('/')
        slug = re.sub(r'[^A-Za-z0-9_.-]+', '_', slug).strip('._')[:80]
        return slug or 'page'

    @staticmethod
    def __write_text(path: str, text: str) -> None:
        with open(path, 'w', encoding='utf-8') as file:
            file.write(text)

    @staticmethod
    def __read_text(path: str) -> str:
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as file:
                return file.read()
        except OSError:
            return ''
