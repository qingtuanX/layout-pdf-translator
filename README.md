# PDF 排版翻译工具

本程序批量翻译本文件夹中的文字型 PDF 论文，并尽量保留原页面尺寸、双栏位置、图片、表格线和图形。原始 PDF 不会被修改；译文写入 `output` 子目录。

程序提取每页的文字块，调用 OpenAI 兼容接口翻译，再用中文字体写回原文字块的位置。数学公式、网址、纯符号等内容会尽量保持原样。译文长度通常和英文不同，程序会自动缩小字号以适应原区域，因此复杂页面仍建议人工校对。

## 目录结构

```text
排版翻译/
├── translate_pdf.py
├── requirements.txt
├── README.md
├── 你的论文.pdf
└── output/
    ├── 你的论文.zh-CN.pdf
    └── .cache/                # 翻译缓存，支持失败后续跑
```

把需要翻译的 PDF 放在本目录或其子目录。程序会递归查找 PDF，并排除 `output` 目录。

## Windows 安装

在 PowerShell 中运行：

```powershell
cd "D:\CODE\网络感知调度\排版翻译"
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

如果不想建立虚拟环境，也可以使用已安装 PyMuPDF 的 Python；虚拟环境更不容易和其他项目的依赖冲突。

## 配置翻译接口

程序使用 OpenAI 兼容的 Chat Completions 接口。PowerShell 中设置：

```powershell
$env:TRANSLATE_API_KEY = "你的API密钥"
$env:TRANSLATE_BASE_URL = "https://api.openai.com/v1"
$env:TRANSLATE_MODEL = "gpt-4o-mini"
```

模型名需按你所用服务实际支持的名称修改。也支持其他 OpenAI 兼容网关，只需将 `TRANSLATE_BASE_URL` 改为服务的 API 根地址，模型名改为服务提供的名称。密钥只从环境变量读取，不会写进程序或缓存；关闭 PowerShell 后临时环境变量会失效。

换服务时一定要同时改 `TRANSLATE_MODEL`：脚本默认值是 `gpt-4o-mini`，若服务不支持该名称，接口会直接返回 `HTTP 400`。例如 DeepSeek 只接受它自己提供的模型名：

```powershell
$env:TRANSLATE_API_KEY = "你的DeepSeek密钥"
$env:TRANSLATE_BASE_URL = "https://api.deepseek.com/v1"
$env:TRANSLATE_MODEL = "deepseek-flash"   # 也可用 deepseek-v4-pro，更慢也更贵
```

本地模型服务如果提供 `/v1/chat/completions` 兼容接口，也可以使用：

```powershell
$env:TRANSLATE_API_KEY = "local"
$env:TRANSLATE_BASE_URL = "http://127.0.0.1:11434/v1"
$env:TRANSLATE_MODEL = "qwen2.5:14b"
```

## 运行

先检查能否提取 PDF 文字，不会调用接口，也不会生成文件：

```powershell
.\.venv\Scripts\python.exe .\translate_pdf.py --dry-run
```

翻译本文件夹中的所有 PDF：

```powershell
.\.venv\Scripts\python.exe .\translate_pdf.py
```

只翻译一个文件：

```powershell
.\.venv\Scripts\python.exe .\translate_pdf.py --input .\2606.03910v1.pdf
```

只翻译第 1 到 3 页和第 5 页：

```powershell
.\.venv\Scripts\python.exe .\translate_pdf.py --input .\2606.03910v1.pdf --pages 1-3,5
```

译文默认位于 `output\文件名.zh-CN.pdf`。输出已存在时默认跳过；要覆盖译文：

```powershell
.\.venv\Scripts\python.exe .\translate_pdf.py --overwrite
```

每个成功翻译的文本块会写入 `output\.cache`。请求超时、连接中断和 429/5xx 会自动重试（指数退避，次数由 `--max-retries` 控制），重试仍失败才终止；此时再次执行即可复用已成功的部分。缓存与模型名绑定，换了 `TRANSLATE_MODEL` 会重新翻译。若想忽略缓存、重新翻译并覆盖输出：

```powershell
.\.venv\Scripts\python.exe .\translate_pdf.py --force --overwrite
```

## 无 API 测试

可先用模拟译文检查 PDF 读写、中文字体和排版覆盖流程：

```powershell
.\.venv\Scripts\python.exe .\translate_pdf.py --input .\2606.03910v1.pdf --mock --overwrite
```

模拟输出只用于检查程序，不是真实翻译。

## 常见选项

- `--source-dir "D:\论文"`：指定扫描目录。
- `--output-dir "D:\译文"`：指定输出目录。
- `--font "C:\Windows\Fonts\simhei.ttf"`：手动指定中文字体。Windows 下默认会自动查找系统字体。
- `--target-language "Traditional Chinese"`：改目标语言。
- `--verbose`：输出调试日志。
- `--max-batch-chars 5000`：缩小每次 API 请求文本量。

运行 `python translate_pdf.py --help` 可查看完整参数。

## 限制与隐私

- 扫描版 PDF 的页面是图片，没有可提取文字时，本程序会提示；请先 OCR 后再翻译。
- 程序尽量保留文字块位置和字号，但英文与中文长度不同，表格、公式周围文字、跨栏标题和脚注可能需要人工检查。
- 对白色页面效果最好。文字若覆盖在彩色底纹或图片上，白色遮罩可能与背景不一致。
- 使用云端接口时，提取出的论文文字会发送给该服务。未公开文档请使用认可的服务或本地模型。
