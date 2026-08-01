# -*- coding: utf-8 -*-

# 本地 html 转 markdown 插件：从落地的 html 里挑出正文，剥掉导航/评论/脚本等噪音，
# 再转成带围栏代码块的 markdown。默认吃 http-request 插件的产物，也可以直接指定本地文件。
import json
import os
import re
from logging import Logger
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from bs4 import BeautifulSoup, NavigableString, Tag
from markdownify import MarkdownConverter, ATX

from engine import PluginCore
from model import Meta, PluginInvokeResponse, PluginContext
from util import FileSystem

# 正文容器候选，按优先级从高到低命中即用
DEFAULT_CONTENT_SELECTORS = [
    'article .markdown-body',
    '.markdown-body',
    '.article-content',
    '.post-content',
    '.entry-content',
    '#content article',
    'article',
    'main',
    '[role="main"]',
]

# 整块丢弃的标签
DROP_TAGS = ['script', 'style', 'noscript', 'template', 'iframe', 'svg', 'canvas',
             'form', 'button', 'nav', 'aside']

# 命中这些 class/id 关键字的容器视为噪音
NOISE_KEYWORDS = ['comment', 'sidebar', 'footer', 'header', 'navbar', 'nav-',
                  'toolbar', 'breadcrumb', 'pagination', 'advert', 'share',
                  'social', 'related', 'recommend', 'subscribe', 'toc',
                  'catalog', 'skip-link', 'back-to-top']

LANGUAGE_CLASS_PATTERN = re.compile(r'^(?:language|lang|highlight|brush|hljs)[-:]([A-Za-z0-9+#_-]+)$')
BLANK_LINES_PATTERN = re.compile(r'\n{3,}')


class ArticleMarkdownConverter(MarkdownConverter):
    """在 markdownify 基础上补两件事：图片地址重写、行内代码里的换行压平"""

    def __init__(self, image_resolver=None, link_resolver=None, **options):
        super().__init__(**options)
        self._image_resolver = image_resolver
        self._link_resolver = link_resolver

    def convert_img(self, el, text, parent_tags):
        if self._image_resolver:
            src = el.get('src')
            if src:
                el['src'] = self._image_resolver(src)
        alt = el.get('alt')
        if alt:
            # alt 里的方括号会把 ![]() 语法切断
            el['alt'] = alt.replace('[', r'\[').replace(']', r'\]')
        return super().convert_img(el, text, parent_tags)

    def convert_a(self, el, text, parent_tags):
        if self._link_resolver:
            href = el.get('href')
            if href:
                el['href'] = self._link_resolver(href)
        return super().convert_a(el, text, parent_tags)

    def convert_code(self, el, text, parent_tags):
        # 行内 code 里若混进换行，markdown 会断行，压成空格
        if 'pre' not in parent_tags:
            text = re.sub(r'\s*\n\s*', ' ', text)
        return super().convert_code(el, text, parent_tags)


class Html2MarkdownPlugin(PluginCore):
    """
    配置项（context.get_config()）：
        html_path         : 可选，本地 html 路径；缺省时取 HttpRequestPlugin 产物的 html_path
        markdown_path     : 可选，markdown 输出全路径
        out_dir           : 可选，markdown 输出目录，默认与 html 同目录
        markdown_name     : 可选，markdown 文件名，默认按标题生成
        content_selectors : 可选，正文容器 css 选择器候选列表
        drop_selectors    : 可选，额外需要剔除的 css 选择器列表
        keep_noise        : 是否保留 nav/comment 等噪音块，默认 False
        image_link        : 'local'(默认，指向本地 assets) / 'remote'(还原成原始网址)
        front_matter      : 是否输出 yaml front matter，默认 True
        title             : 可选，强制指定标题
        source_url        : 可选，写进 front matter 的来源地址
        heading_style     : markdownify 标题风格，默认 'atx'
        bullets           : markdownify 无序列表符号，默认 '-'
        wrap              : 是否按宽度折行，默认 False
    """

    def __init__(self, logger: Logger, meta: Meta) -> None:
        super().__init__(logger, meta)
        self._conf: Dict = {}
        # 本地资源名 -> 原始网址，image_link='remote' 时用来还原
        self._local_to_url: Dict[str, str] = {}

    def invoke(self, context: PluginContext) -> PluginInvokeResponse:
        super(Html2MarkdownPlugin, self).invoke(context)
        self._conf = context.get_config()

        upstream = context.get_plugin_data('HttpRequestPlugin') or {}
        html_path = self._conf.get('html_path') or upstream.get('html_path')
        if not html_path:
            return PluginInvokeResponse.create_failed_response(
                -1, 'config error: html_path is empty and no HttpRequestPlugin data found')
        if not os.path.isfile(html_path):
            return PluginInvokeResponse.create_failed_response(-1, f'html not found: {html_path}')

        self._local_to_url = {name: url for url, name in (upstream.get('url_to_local') or {}).items()}
        source_url = self._conf.get('source_url') or upstream.get('final_url') or upstream.get('url', '')

        with open(html_path, 'r', encoding='utf-8', errors='ignore') as file:
            html = file.read()
        self._logger.info(f'read html: {html_path} ({len(html)} chars)')

        soup = BeautifulSoup(html, 'lxml')
        title = self._conf.get('title') or self.__extract_title(soup, upstream)

        content, matched_selector = self.__pick_content(soup)
        if content is None:
            return PluginInvokeResponse.create_failed_response(-1, 'can not locate article content in html')
        self._logger.info(f'article content matched by: {matched_selector}')

        self.__clean(content)
        body = self.__to_markdown(content)
        body = self.__polish(body, title)
        if not body.strip():
            return PluginInvokeResponse.create_failed_response(-1, 'markdown content is empty after convert')

        markdown = self.__build_front_matter(title, source_url) + body
        markdown_path = self.__resolve_output_path(html_path, title)
        FileSystem.create_folder(os.path.dirname(markdown_path))
        with open(markdown_path, 'w', encoding='utf-8') as file:
            file.write(markdown)

        # 转换过程中 src 已被重写，此时取到的就是 markdown 里的最终地址
        images = [img.get('src') for img in content.find_all('img') if img.get('src')]
        result = {
            'title': title,
            'source_url': source_url,
            'html_path': html_path,
            'markdown_path': markdown_path,
            'content_selector': matched_selector,
            'char_count': len(markdown),
            'line_count': markdown.count('\n') + 1,
            'code_block_count': body.count('\n```') // 2,
            'image_count': len(images),
            'images': images,
        }
        self._logger.info(f'markdown saved: {markdown_path} '
                          f'({result["char_count"]} chars, {result["code_block_count"]} code blocks, '
                          f'{result["image_count"]} images)')
        return PluginInvokeResponse.create_succeed_response(result)

    # --------------------------------------------------------------- extract

    def __extract_title(self, soup: BeautifulSoup, upstream: Dict) -> str:
        for tag in soup.select('article h1, main h1, h1'):
            text = tag.get_text(strip=True)
            if text:
                return text
        meta_tag = soup.find('meta', attrs={'property': 'og:title'})
        if meta_tag and meta_tag.get('content'):
            return meta_tag['content'].strip()
        title = upstream.get('title') or (soup.title.get_text(strip=True) if soup.title else '')
        # 站点标题常见形如 "文章名 - 作者 - 站点"，只取第一段
        return re.split(r'\s+[-|｜|]\s+', title)[0].strip() if title else 'untitled'

    def __pick_content(self, soup: BeautifulSoup) -> Tuple[Optional[Tag], str]:
        selectors = self._conf.get('content_selectors') or DEFAULT_CONTENT_SELECTORS
        for selector in selectors:
            for node in soup.select(selector):
                if len(node.get_text(strip=True)) >= 200:
                    return node, selector
        fallback = self.__pick_densest_block(soup)
        if fallback is not None:
            return fallback, 'auto:densest-block'
        return (soup.body or soup), 'fallback:body'

    @staticmethod
    def __pick_densest_block(soup: BeautifulSoup) -> Optional[Tag]:
        """选正文密度最高的块：正文标签文本多、链接文本占比低"""
        best_node, best_score = None, 0.0
        for node in soup.find_all(['div', 'section', 'article', 'main']):
            text_length = len(node.get_text(strip=True))
            if text_length < 200:
                continue
            link_length = sum(len(a.get_text(strip=True)) for a in node.find_all('a'))
            paragraphs = len(node.find_all(['p', 'pre', 'h1', 'h2', 'h3', 'h4', 'li', 'blockquote']))
            score = text_length * (1 - min(link_length / text_length, 1.0)) * (1 + paragraphs * 0.1)
            if score > best_score:
                best_node, best_score = node, score
        return best_node

    def __clean(self, content: Tag) -> None:
        for tag in content.find_all(DROP_TAGS):
            if not tag.decomposed:
                tag.decompose()

        for selector in (self._conf.get('drop_selectors') or []):
            for tag in content.select(selector):
                if not tag.decomposed:
                    tag.decompose()

        if not self._conf.get('keep_noise', False):
            for tag in content.find_all(True):
                # find_all 返回的是快照，父节点被摘掉后子节点已失效，跳过
                if tag.decomposed:
                    continue
                if self.__is_noise(tag):
                    tag.decompose()

        # display:none / hidden 的节点在页面上看不到，markdown 也不该有
        for tag in content.find_all(True):
            if tag.decomposed:
                continue
            if tag.has_attr('hidden') or tag.get('aria-hidden') == 'true':
                tag.decompose()
                continue
            style = (tag.get('style') or '').replace(' ', '').lower()
            if 'display:none' in style or 'visibility:hidden' in style:
                tag.decompose()

        # 标题上的锚点链接（"#"）转成纯文本，否则 markdown 里全是空链接
        for tag in content.find_all('a'):
            if tag.decomposed:
                continue
            href = (tag.get('href') or '').strip()
            if not href or href.startswith('#') or href.startswith('javascript:'):
                tag.replace_with(NavigableString(tag.get_text()))

        # 代码块常见的"复制"按钮、行号栏
        for tag in content.select('.copy-button, .code-copy, .line-numbers-rows, .lineno'):
            if not tag.decomposed:
                tag.decompose()

        self.__normalize_pre(content)

    @staticmethod
    def __normalize_pre(content: Tag) -> None:
        """
        有的高亮组件把每一行代码包成一个 span，换行是靠 css 排出来的，
        pre 的文本里一个换行符都没有，直接转 markdown 会把整段代码挤成一行，这里按行容器补回换行。
        """
        for pre in content.find_all('pre'):
            if '\n' in pre.get_text():
                continue
            holder = pre.find('code') or pre
            rows = [child for child in holder.children if isinstance(child, Tag)]
            if len(rows) < 2:
                continue
            for row in rows[:-1]:
                row.append(NavigableString('\n'))

    @staticmethod
    def __is_noise(tag: Tag) -> bool:
        if tag.name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'pre', 'code', 'table'):
            return False
        # 代码块内部全是高亮用的 class，比如 hljs-comment 会撞上 comment 关键字，一律放行
        if tag.find_parent(['pre', 'code']) is not None:
            return False
        identity = ' '.join(filter(None, [
            ' '.join(tag.get('class') or []),
            tag.get('id') or '',
            tag.get('role') or '',
        ])).lower()
        if not identity:
            return False
        return any(keyword in identity for keyword in NOISE_KEYWORDS)

    # --------------------------------------------------------------- convert

    def __to_markdown(self, content: Tag) -> str:
        converter = ArticleMarkdownConverter(
            image_resolver=self.__resolve_image,
            link_resolver=self.__resolve_link,
            heading_style=self._conf.get('heading_style', ATX),
            bullets=self._conf.get('bullets', '-'),
            code_language_callback=self.__detect_language,
            wrap=self._conf.get('wrap', False),
            escape_asterisks=False,
            escape_underscores=False,
            escape_misc=False,
            strip_document='strip',
        )
        return converter.convert_soup(content)

    @staticmethod
    def __detect_language(el: Tag) -> str:
        """从 pre / code 的 class、data-lang 上猜出代码块语言"""
        candidates: List[Tag] = [el]
        code = el.find('code') if el.name == 'pre' else None
        if code is not None:
            candidates.append(code)
        for node in candidates:
            for attr in ('data-lang', 'data-language', 'lang'):
                value = (node.get(attr) or '').strip()
                if value:
                    return value.lower()
            for css_class in (node.get('class') or []):
                match = LANGUAGE_CLASS_PATTERN.match(css_class.strip())
                if match and match.group(1).lower() not in ('none', 'plain', 'plaintext'):
                    return match.group(1).lower()
        return ''

    def __resolve_image(self, src: str) -> str:
        src = (src or '').strip()
        if self._conf.get('image_link', 'local') != 'remote':
            return src
        # remote 模式：把 assets/xxx 还原回原始网址，方便 markdown 脱离本地目录使用
        if src.startswith('assets/'):
            return self._local_to_url.get(src[len('assets/'):], src)
        return src

    def __resolve_link(self, href: str) -> str:
        return (href or '').strip()

    # ---------------------------------------------------------------- output

    def __polish(self, markdown: str, title: str) -> str:
        lines = [line.rstrip() for line in markdown.split('\n')]
        markdown = '\n'.join(lines)
        markdown = BLANK_LINES_PATTERN.sub('\n\n', markdown).strip()
        # 正文首行如果就是标题，去掉以免和 front matter / H1 重复
        first_line = markdown.split('\n', 1)[0].strip()
        if title and first_line.lstrip('# ').strip() == title.strip():
            markdown = markdown.split('\n', 1)[1].strip() if '\n' in markdown else ''
        return f'# {title}\n\n{markdown}\n' if title else markdown + '\n'

    def __build_front_matter(self, title: str, source_url: str) -> str:
        if not self._conf.get('front_matter', True):
            return ''
        fields = [('title', title), ('source', source_url)]
        host = urlparse(source_url).netloc if source_url else ''
        if host:
            fields.append(('site', host))
        body = '\n'.join(f'{key}: {json.dumps(value, ensure_ascii=False)}'
                         for key, value in fields if value)
        return f'---\n{body}\n---\n\n'

    def __resolve_output_path(self, html_path: str, title: str) -> str:
        markdown_path = self._conf.get('markdown_path')
        if markdown_path:
            return markdown_path
        out_dir = self._conf.get('markdown_out_dir') or os.path.dirname(html_path)
        name = self._conf.get('markdown_name') or f'{self.__safe_name(title)}.md'
        return os.path.join(out_dir, name)

    @staticmethod
    def __safe_name(title: str) -> str:
        name = re.sub(r'[\\/:*?"<>|\s]+', '_', (title or 'article').strip()).strip('._')
        return name[:80] or 'article'
