# -*- coding: utf-8 -*-

# 网页转 markdown：http-request 插件把页面和资源抓到本地，html2markdown 插件再把本地 html 转成 markdown
import argparse
import os
import sys

root_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
if root_path not in sys.path:
    sys.path.insert(0, root_path)

from engine import PluginEngineFactory
from util import FileSystem


def parse_args():
    parser = argparse.ArgumentParser(description='把网页文章转成 markdown')
    parser.add_argument('url', nargs='?', help='目标网页地址，缺省时用 config.yaml 里的 url')
    parser.add_argument('-c', '--config', default='config.yaml', help='配置文件名，默认 config.yaml')
    parser.add_argument('-o', '--out-dir', help='输出目录，覆盖配置文件里的 out_dir')
    parser.add_argument('--render', choices=['auto', 'true', 'false'],
                        help='是否用无头浏览器渲染 js，覆盖配置文件')
    parser.add_argument('--no-assets', action='store_true', help='只抓 html，不下载 css/js/图片等资源')
    parser.add_argument('--image-link', choices=['local', 'remote'],
                        help='markdown 里图片指向本地 assets 还是原始网址')
    parser.add_argument('--clean', action='store_true', help='开跑前清空输出目录')
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    current_path = os.path.abspath(os.path.dirname(__file__))
    config_data = FileSystem.load_configuration(args.config, current_path)

    if args.url:
        config_data['url'] = args.url
    if args.out_dir:
        config_data['out_dir'] = args.out_dir
    if args.render:
        config_data['render'] = {'auto': 'auto', 'true': True, 'false': False}[args.render]
    if args.no_assets:
        config_data['download_assets'] = False
    if args.image_link:
        config_data['image_link'] = args.image_link

    out_dir = config_data.get('out_dir', './out')
    if not os.path.isabs(out_dir):
        out_dir = os.path.join(current_path, out_dir)
    out_dir = os.path.abspath(out_dir)
    if args.clean:
        FileSystem.reset_folder(out_dir)
    else:
        FileSystem.create_folder(out_dir)
    config_data['out_dir'] = out_dir

    plugin_engine = PluginEngineFactory.create_html2markdown_engine()
    plugin_engine.get_context().set_config(config_data)
    plugin_engine.invoke()

    context = plugin_engine.get_context()
    page = context.get_plugin_data('HttpRequestPlugin') or {}
    article = context.get_plugin_data('Html2MarkdownPlugin') or {}

    print('\n================ 转换结果 ================')
    print(f"来源      : {config_data['url']}")
    if page:
        print(f"页面标题  : {page.get('title', '')}")
        print(f"渲染方式  : {'无头浏览器' if page.get('render_used') else '静态请求'}")
        print(f"本地 html : {page.get('html_path', '')}")
        print(f"资源      : 成功 {len(page.get('assets') or [])} 个，失败 {len(page.get('failed_assets') or [])} 个")
    if article:
        print(f"文章标题  : {article.get('title', '')}")
        print(f"markdown  : {article.get('markdown_path', '')}")
        print(f"正文规模  : {article.get('char_count', 0)} 字符 / "
              f"{article.get('code_block_count', 0)} 个代码块 / {article.get('image_count', 0)} 张图")
    else:
        print('markdown  : 转换失败，详见上方日志')
        sys.exit(1)
    print('==========================================')
