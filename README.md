+ plugins：业务插件集合（插件目录直接挂在 plugins 下，也支持 `分组/插件名` 的两级形式）
    + http-request：下载网页 html 及其全部资源（css / js / 图片 / 字体 / 媒体）到本地
        + 静态请求与无头浏览器渲染双模式，auto 模式下自动识别前端渲染页面并回退到渲染
        + 多线程下载资源，递归解析 css 内的 url() / @import，把字体、背景图一并落地
        + 把 html 与 css 里的引用改写成本地相对路径，产出可脱网打开的副本
    + html2markdown：把本地 html 转成 markdown
        + 正文容器自动定位（选择器优先，失败时按文本密度挑选）
        + 剔除导航、评论、侧栏、隐藏元素等噪音，保留代码块内的高亮结构
        + 代码块还原围栏语法并识别语言，兼容「每行一个 span」的高亮组件
        + 图片可指向本地 assets，也可还原成原始网址

+ bin/html2markdown：网页文章转 markdown 命令行

```bash
# 按 config.yaml 抓取并转换
python bin/html2markdown/html2markdown-cli.py

# 指定网址，清空输出目录后重跑
python bin/html2markdown/html2markdown-cli.py https://example.com/post/1 --clean

# 只要 html 不要资源，且 markdown 里的图片保留原始网址
python bin/html2markdown/html2markdown-cli.py --no-assets --image-link remote
```

产物落在 `bin/html2markdown/out/<站点_路径>/`：

```
raw.html      # 抓到的原始 html
index.html    # 资源引用改写为本地路径后的副本
meta.json     # 抓取明细：最终地址、资源清单、url -> 本地文件映射
assets/       # 全部资源
<标题>.md     # 最终 markdown
```

+ 环境准备

```bash
pip install -r requirements.txt
playwright install chromium   # 仅 render 模式需要
```
