# PDF 排版翻译工具

本程序批量翻译本文件夹中的文字型 PDF 论文，并尽量保留原页面尺寸、双栏位置、图片、表格线和图形。原始 PDF 不会被修改；译文写入 `output` 子目录。

程序提取每页的文字块，调用 OpenAI 兼容接口翻译，再用中文字体写回原文字块的位置。表格和独立公式会被自动识别出来，直接从原始页面截图贴回原位（表格标题仍翻译成中文），这样单元格不会错位、公式也不会被改坏；网址、纯符号等内容保持原样。译文长度通常和英文不同，程序会自动缩小字号以适应原区域，因此复杂页面仍建议人工校对。

## 下载即用（Windows 10/11 64 位，不用装 Python）

不想装 Python 就走这条：去 [Releases](https://github.com/qingtuanX/layout-pdf-translator/releases) 下载最新的 `PDF-Translator-*.exe`，双击打开，把 PDF 拖进窗口就会自动生成中文译文（译文落在原 PDF 旁边）。界面、拖拽、排队、表格公式截图保留都在里面。

第一次打开点「设置」，填你自己的 **API 地址、模型名和密钥** —— 程序不带密钥，默认地址是 OpenAI 的；用 DeepSeek 就填 `https://api.deepseek.com/v1` + `deepseek-flash`。设置和译文缓存都只存在你本机。

> 首次运行 Windows 可能提示「未知发布者」（SmartScreen），点「更多信息 → 仍要运行」即可。另外：程序会把 PDF 里的文字发给你在设置里填的那个服务，未公开文档请用你信任的服务或本地模型。

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

## 图形界面（拖拽版）

`dist\翻译器.exe` 双击打开，把 PDF 拖进窗口就会自动开始翻译，译文生成在**原文件旁边**（`原名.zh-CN.pdf`）。可以一次拖多个文件，也可以拖整个文件夹（递归找 `*.pdf`）；任务是排队逐个跑的，单个文件失败只标红、不影响队列后面的文件。把 PDF 直接拖到 exe 图标上，或者在命令行里写 `翻译器.exe 论文1.pdf 论文2.pdf`，效果一样。

窗口里：任务列表显示 文件 / 状态 / 输出，双击已完成的行会用默认程序打开译文；下方日志区实时显示识别到的表格公式区域和翻译进度；「停止」在批次之间生效（已翻好的部分进缓存，下次接着跑）。

点「设置」填 API 地址、模型名和密钥（存到 `%APPDATA%\layout-pdf-translator\config.json`，明文，只在本机），译文位置可选「原文件旁边」或「统一放到」某个目录，还能选择是否覆盖已存在的译文。翻译缓存在 `%LOCALAPPDATA%\layout-pdf-translator\cache`；如果文件旁边已经有命令行留下的 `output\.cache`，会优先复用它，不会重复花钱。

改了代码要重新打包（需要先 `pip install pyinstaller`）：

```powershell
.\build_exe.bat
```

## 命令行运行

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
- `--region-dpi 300`：表格/公式截图的分辨率，默认 200；觉得截图发虚可以调高。
- `--no-screenshot`：关掉截图逻辑，退回逐块替换文本（表格和公式按普通文本处理，就是以前的行为）。

先用 `--dry-run` 可以看到识别出的表格/公式区域和它们覆盖的文本块，确认无误再真正翻译。

运行 `python translate_pdf.py --help` 可查看完整参数。

## 限制与隐私

- 扫描版 PDF 的页面是图片，没有可提取文字时，本程序会提示；请先 OCR 后再翻译。
- 表格/公式靠版面启发式识别：表格靠水平线聚类（过窄的短线当作公式分式线排除），公式靠 Computer Modern 一类数学字体占比加数学符号。换排版风格可能需要调整 `TABLE_MIN_WIDTH`、`RULE_GAP` 这些阈值。公式密集的段落会整段保留英文。
- 截图区域在译文里是图片：不可选中、不可搜索，字号也无法调整；文件会相应增大。
- 译文用 HTML 引擎渲染，中文字体缺的数学符号（ℓ、∆、∗、ϵ、µ 等）会自动回退到内置字体，不再写成空框。个别符号连内置字体也没有（如数学的帽子 ˆ），会换成等价字形（^）——`RENDER_SUBSTITUTIONS` 里可以增删。
- 相比逐块纯文本替换，HTML 渲染更慢、文件更大：整篇约 10-15 秒，译文 PDF 会多出 2MB 左右（多出来的是回退字体）。
- 程序尽量保留文字块位置和字号，但英文与中文长度不同，跨栏标题和脚注可能需要人工检查。
- 对白色页面效果最好。文字若覆盖在彩色底纹或图片上，白色遮罩可能与背景不一致。
- 使用云端接口时，提取出的论文文字会发送给该服务。未公开文档请使用认可的服务或本地模型。
