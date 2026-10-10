#!/usr/bin/env python3
"""Generate the installed LingJingAPI Chinese user and API guide."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    Image,
    PageBreak,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "assets" / "LingJingAPI使用教学.pdf"
CLIENT_VERSION = (ROOT / "VERSION").read_text(encoding="utf-8-sig").strip()
RUNTIME_RELEASE = json.loads(
    (ROOT / "app" / "runtime_release.json").read_text(encoding="utf-8-sig")
)
RUNTIME_PACKAGE_NAME = str(RUNTIME_RELEASE["package_name"])
PROJECT_URL = str(RUNTIME_RELEASE["homepage_url"])
ACCENT = colors.HexColor("#5545FC")
ACCENT_DARK = colors.HexColor("#3844E1")
TEXT = colors.HexColor("#172033")
TEXT_2 = colors.HexColor("#52657F")
BORDER = colors.HexColor("#D7E2F4")
SOFT = colors.HexColor("#F4F6FF")
SOFT_WARN = colors.HexColor("#FFF5E6")


def register_fonts() -> None:
    font_dir = Path("C:/Windows/Fonts")
    pdfmetrics.registerFont(
        TTFont("LingJing", str(font_dir / "msyh.ttc"), subfontIndex=0)
    )
    pdfmetrics.registerFont(
        TTFont("LingJingBold", str(font_dir / "msyhbd.ttc"), subfontIndex=0)
    )
    pdfmetrics.registerFontFamily(
        "LingJing",
        normal="LingJing",
        bold="LingJingBold",
        italic="LingJing",
        boldItalic="LingJingBold",
    )


def styles():
    base = getSampleStyleSheet()
    return {
        "cover_title": ParagraphStyle(
            "CoverTitle",
            parent=base["Title"],
            fontName="LingJingBold",
            fontSize=25,
            leading=34,
            textColor=TEXT,
            alignment=TA_CENTER,
            spaceAfter=6 * mm,
        ),
        "cover_subtitle": ParagraphStyle(
            "CoverSubtitle",
            parent=base["Normal"],
            fontName="LingJing",
            fontSize=11,
            leading=18,
            textColor=TEXT_2,
            alignment=TA_CENTER,
        ),
        "h1": ParagraphStyle(
            "H1",
            parent=base["Heading1"],
            fontName="LingJingBold",
            fontSize=19,
            leading=26,
            textColor=TEXT,
            spaceBefore=2 * mm,
            spaceAfter=5 * mm,
        ),
        "h2": ParagraphStyle(
            "H2",
            parent=base["Heading2"],
            fontName="LingJingBold",
            fontSize=13,
            leading=19,
            textColor=ACCENT_DARK,
            spaceBefore=4 * mm,
            spaceAfter=2.5 * mm,
        ),
        "body": ParagraphStyle(
            "Body",
            parent=base["BodyText"],
            fontName="LingJing",
            fontSize=9.5,
            leading=16,
            textColor=TEXT,
            wordWrap="CJK",
            spaceAfter=2.4 * mm,
        ),
        "small": ParagraphStyle(
            "Small",
            parent=base["BodyText"],
            fontName="LingJing",
            fontSize=8,
            leading=12,
            textColor=TEXT_2,
            wordWrap="CJK",
        ),
        "step": ParagraphStyle(
            "Step",
            parent=base["BodyText"],
            fontName="LingJing",
            fontSize=9.5,
            leading=16,
            leftIndent=6 * mm,
            firstLineIndent=-6 * mm,
            textColor=TEXT,
            wordWrap="CJK",
            spaceAfter=2.6 * mm,
        ),
        "code": ParagraphStyle(
            "Code",
            parent=base["Code"],
            fontName="LingJing",
            fontSize=7.5,
            leading=11,
            textColor=colors.HexColor("#24314A"),
            leftIndent=0,
            rightIndent=0,
            spaceBefore=0,
            spaceAfter=0,
        ),
        "table_head": ParagraphStyle(
            "TableHead",
            parent=base["BodyText"],
            fontName="LingJingBold",
            fontSize=8,
            leading=11,
            textColor=colors.white,
            alignment=TA_LEFT,
        ),
        "table_cell": ParagraphStyle(
            "TableCell",
            parent=base["BodyText"],
            fontName="LingJing",
            fontSize=7.5,
            leading=11,
            textColor=TEXT,
            wordWrap="CJK",
        ),
    }


def note_box(text: str, style, background=SOFT):
    table = Table([[Paragraph(text, style)]], colWidths=[166 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), background),
                ("BOX", (0, 0), (-1, -1), 0.8, BORDER),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return table


def code_box(code: str, style):
    block = Preformatted(code.strip(), style)
    table = Table([[block]], colWidths=[166 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F7F9FC")),
                ("BOX", (0, 0), (-1, -1), 0.7, BORDER),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return table


def endpoint_table(s):
    rows = [
        ["方法", "路径", "用途"],
        ["GET", "/healthz", "免鉴权的最小存活状态"],
        ["GET", "/v1/status", "客户端、环境和后台服务完整状态"],
        ["GET", "/v1/models", "当前可调用模型列表"],
        ["GET", "/v1/workflows", "工作流与公开调用名称列表"],
        ["POST", "/", "运行当前选中的默认工作流"],
        ["POST", "/{workflow_alias}", "按公开名称运行指定工作流"],
        ["POST", "/v1/chat/completions", "OpenAI 风格文字生成接口"],
        ["POST", "/api/v3/images/generations", "火山/即梦风格图片生成接口"],
        ["POST", "/v1/workflows/run/{workflow_id}", "按工作流 ID 提交生成任务"],
        ["GET", "/v1/tasks/{task_id}", "查询异步任务进度和结果"],
        ["GET", "/v1/tasks/queue", "查看统一队列与排队位置"],
        ["POST", "/v1/tasks/{task_id}/cancel", "取消等待或运行中的任务"],
        ["GET", "/v1/files/{task_id}/{filename}", "下载任务生成文件"],
    ]
    rendered = []
    for index, row in enumerate(rows):
        rendered.append(
            [Paragraph(value, s["table_head"] if index == 0 else s["table_cell"]) for value in row]
        )
    table = Table(rendered, colWidths=[18 * mm, 66 * mm, 82 * mm], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), ACCENT_DARK),
                ("GRID", (0, 0), (-1, -1), 0.45, BORDER),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFD")]),
            ]
        )
    )
    return table


def draw_page_frame(canvas, doc):
    canvas.saveState()
    width, height = A4
    canvas.setStrokeColor(BORDER)
    canvas.setLineWidth(0.5)
    if doc.page > 1:
        canvas.line(22 * mm, height - 15 * mm, width - 22 * mm, height - 15 * mm)
        canvas.setFont("LingJing", 7.5)
        canvas.setFillColor(TEXT_2)
        canvas.drawString(22 * mm, height - 11.5 * mm, "LingJingAPI 使用教学与接口说明")
    canvas.line(22 * mm, 14 * mm, width - 22 * mm, 14 * mm)
    canvas.setFont("LingJing", 7.5)
    canvas.setFillColor(TEXT_2)
    canvas.drawString(22 * mm, 9.5 * mm, f"版本 {CLIENT_VERSION}  |  Windows 10/11 x64")
    canvas.drawRightString(width - 22 * mm, 9.5 * mm, f"第 {doc.page} 页")
    canvas.restoreState()


def build_story(s):
    story = []
    logo = ROOT / "icon.png"
    if logo.is_file():
        image = Image(str(logo), width=28 * mm, height=28 * mm)
        image.hAlign = "CENTER"
        story.extend([Spacer(1, 18 * mm), image, Spacer(1, 8 * mm)])
    story.append(Paragraph("LingJingAPI", s["cover_title"]))
    story.append(Paragraph("一键调用算力，简单好用。", s["cover_subtitle"]))
    story.append(Paragraph("使用教学与 API 接口说明", s["cover_subtitle"]))
    story.append(Spacer(1, 12 * mm))
    story.append(
        note_box(
            "这份文档面向首次安装用户和接口接入人员。客户端、AI 运行环境、模型和用户数据彼此分离；安装客户端后可一键拉取运行环境，只有网络失败时才需要手动下载。",
            s["body"],
        )
    )
    story.append(Spacer(1, 10 * mm))
    summary = [
        [Paragraph("项目主页", s["table_head"]), Paragraph(PROJECT_URL, s["table_cell"])],
        [Paragraph("客户端版本", s["table_head"]), Paragraph(CLIENT_VERSION, s["table_cell"])],
        [Paragraph("支持系统", s["table_head"]), Paragraph("Windows 10 22H2 / Windows 11，64 位", s["table_cell"])],
        [Paragraph("支持硬件", s["table_head"]), Paragraph("NVIDIA RTX 20 系列或更高，显存 8GB 或以上；8GB 仅适合轻量工作流，CUDA 13 环境要求 R580 或更高驱动", s["table_cell"])],
    ]
    info = Table(summary, colWidths=[38 * mm, 128 * mm])
    info.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), ACCENT_DARK),
                ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    story.append(info)
    story.append(Spacer(1, 22 * mm))
    story.append(Paragraph("Yaro-lu  |  本地 AI 工作流服务", s["cover_subtitle"]))

    story.extend([PageBreak(), Paragraph("1. 安装与第一次启动", s["h1"])])
    story.append(Paragraph("1. 双击轻量安装包，按提示完成安装。安装过程不需要管理员权限，也不会安装模型。", s["step"]))
    story.append(Paragraph("2. 安装完成后，桌面会出现两个使用同一 Logo 的入口：“LingJingAPI”用于启动客户端，“LingJingAPI 示例页”用于第一次体验接口。", s["step"]))
    story.append(Paragraph("3. 默认打开新手模式：环境或 Qwen3.5 缺失时，点击“一键修复”，自动准备运行环境和默认图文视频模型。已有可用环境会直接启动服务，不要求重新完成引导。", s["step"]))
    story.append(Paragraph("4. 修复前会显示环境与模型的完整下载量和本次剩余量，全新安装约下载 52.06 GB 或 67.30 GB。已有模型与断点会复用，进度按实际字节计算；下载完成后仍需安装、校验与启动验证，安装后磁盘占用另计。", s["step"]))
    story.append(Paragraph("5. 等待状态显示“启动成功”，点击“开始创作”，自动打开并连接示例页面。其他电脑或手机通过 URL + Key 调用。顶部居中切换新手 / 专家模式；专家模式保留原控制台、模型管理和设置。", s["step"]))
    story.append(Paragraph("自动下载失败时按弹窗中的备用网盘提示手动下载环境包，再导入本地环境包。大文件 SHA256 校验只在修复时进行，启动检测保持轻量。", s["body"]))
    story.append(Spacer(1, 3 * mm))
    story.append(note_box(f"运行环境包文件名：<b>{RUNTIME_PACKAGE_NAME}</b><br/>项目主页：<b>{PROJECT_URL}</b>", s["body"], SOFT_WARN))
    story.append(Paragraph("安装目录中的主要入口", s["h2"]))
    entry_rows = [
        ["名称", "用途"],
        ["LingJingAPI", "正式启动入口，使用项目 Logo"],
        ["LingJingAPI示例页.html", "本地纯前端新手页面；填写 URL 和 Key 后自动检测模型并调用生成"],
        ["LingJingAPI使用教学.pdf", "当前使用教学与接口说明"],
        ["models", "用户模型目录，更新客户端时保留"],
        ["outputs", "生成结果目录"],
        ["workflows", "内置或导入的工作流定义"],
    ]
    entry_table = Table(
        [[Paragraph(v, s["table_head"] if i == 0 else s["table_cell"]) for v in row] for i, row in enumerate(entry_rows)],
        colWidths=[55 * mm, 111 * mm],
        repeatRows=1,
    )
    entry_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), ACCENT_DARK), ("GRID", (0, 0), (-1, -1), 0.5, BORDER), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story.append(entry_table)

    screenshot = ROOT / "assets" / "readme" / "beginner-mode.png"
    if screenshot.is_file():
        story.extend([PageBreak(), Paragraph("图解 · 新手模式", s["h1"])])
        picture = Image(str(screenshot))
        picture.drawHeight *= 166 * mm / picture.drawWidth
        picture.drawWidth = 166 * mm
        story.append(picture)
        story.append(Spacer(1, 5 * mm))
        story.append(Paragraph("无需填写配置：一键准备环境与模型，启动后提供 URL 与生成 Key。已有可用环境直接启动，截图中的首次引导提示以实际检测结果为准。需要更多管理功能时，切换顶部“专家模式”。", s["body"]))

    story.extend([PageBreak(), Paragraph("2. 环境、模型与工作流", s["h1"])])
    story.append(Paragraph("运行环境", s["h2"]))
    story.append(Paragraph("运行环境包含便携 Python、ComfyUI、Torch/CUDA 和 Cloudflared。客户端会先校验环境包名称、精确大小、SHA256 和目录结构，再事务替换环境目录。模型、工作流和生成结果不会因为环境修复而删除。", s["body"]))
    story.append(Paragraph("官方环境包版本、文件名、下载地址、大小和 SHA256 固定在客户端发布清单中。需要使用授权镜像时，可以在 runtime/config.local.txt 的 [runtime] 区域修改 HTTPS download_url；镜像不能改变客户端内置的文件名、大小和 SHA256 校验。", s["body"]))
    story.append(Paragraph("模型", s["h2"]))
    story.append(Paragraph("模型默认放在安装根目录的 models 文件夹，也可在“模型文件”中映射已有模型。修改目录后会扫描并适配相同的目录结构。优先使用可信来源的 safetensors 或 GGUF 文件，不要导入来源不明的传统 PyTorch checkpoint。", s["body"]))
    story.append(Paragraph("工作流", s["h2"]))
    story.append(Paragraph("每个工作流至少包含 manifest.json 和 workflow.json。manifest.json 定义公开调用名称、输入参数、输出类型和模型依赖。导入后应在“模型与环境”的工作流列表确认状态为“可以使用”。", s["body"]))
    story.append(Paragraph("专家模式的操作顺序", s["h2"]))
    for index, text in enumerate(
        [
            "安装或修复运行环境，并执行“检查环境”。",
            "导入工作流所需模型，然后点击“重新检查”。",
            "在“模型与环境”的工作流列表选择默认工作流。",
            "回到控制台启动后台服务，复制 URL 和 API Key。",
            "在浏览器直接打开控制台的本地、局域网或公网 URL，填写 Key，连接后选择可用工作流。原有独立 HTML 页面也可以继续使用。",
        ],
        1,
    ):
        story.append(Paragraph(f"{index}. {text}", s["step"]))
    story.append(note_box("客户端更新只替换程序文件；运行环境、模型、配置、日志和生成结果独立保存。卸载不会递归删除模型、生成结果、后装运行环境、用户配置和导入的工作流；完成后会提示模型与生成结果的位置，可自行检查并删除。重要内容仍请独立备份。", s["body"]))

    for title, filename, caption in [
        ("图解 1 · 工作流、模型与环境", "workflow-models.png", "演示界面，状态数值为演示数据。工作流按钮集中在右侧，缺失模型可下载或映射已有文件。"),
        ("图解 2 · 连接客户端", "console.png", "演示界面，公网地址与 Key 已替换为占位信息。本机、同 Wi-Fi 和公网分别使用对应的 URL。"),
        ("图解 3 · 网页创作", "studio.png", "实际创作页面：左侧管理项目，中间查看作品，右侧选择工作流与生成参数。图片点击加载原图，视频先加载首帧预览。"),
    ]:
        screenshot = ROOT / "docs" / "images" / "2.0" / filename
        if screenshot.is_file():
            story.extend([PageBreak(), Paragraph(title, s["h1"])])
            picture = Image(str(screenshot))
            picture.drawHeight *= 166 * mm / picture.drawWidth
            picture.drawWidth = 166 * mm
            story.append(picture)
            story.append(Spacer(1, 5 * mm))
            story.append(Paragraph(caption, s["body"]))
            if filename == "studio.png":
                story.append(Paragraph("尝试：使用 FLUX.2 Klein 4B 输入“一只猫坐在杯子里”，生成后将图片加入参考图，再输入“把杯子换成红色”。这是操作示例，每次生成画面可能不同。", s["body"]))
                story.append(Paragraph("项目分类和历史保存在浏览器，切换 URL 或 Key 不会主动清空记录。删除项目仅移除分类；作品菜单中的“删除文件”会删除对应本地文件。当前按单用户使用，不做 Key 之间的作品隔离。", s["body"]))

    story.extend([PageBreak(), Paragraph("3. API 接入基础", s["h1"])])
    story.append(Paragraph("新手示例页", s["h2"]))
    story.append(Paragraph("“LingJingAPI示例页.html”完全保存在本机，是一个不依赖额外服务的纯前端页面。可直接通过客户端 URL 打开，也可使用独立 HTML 文件。连接后只显示当前可用的工作流，选择后按该工作流加载参数。", s["body"]))
    story.append(note_box("在网页右上角填写 URL 和 API Key 后连接，右侧选择工作流并输入提示词。视频输入按工作流要求选择首帧、尾帧或参考图。连接信息会保存在当前浏览器，刷新时尝试自动连接一次，请在自己的设备上使用。", s["body"]))
    story.append(Paragraph("地址与鉴权", s["h2"]))
    story.append(Paragraph("控制台会显示公网、本地及局域网 BASE_URL，以及生成接口使用的 API_KEY。除 `/healthz` 外，业务接口通常需要在请求头中携带 Bearer Token。本机和支持的私有局域网地址可使用 HTTP，公网地址必须使用 HTTPS。手机建议在浏览器直接打开局域网 URL，并确保同一网络和 Windows 防火墙允许访问。", s["body"]))
    story.append(code_box('Authorization: Bearer API_KEY\nContent-Type: application/json', s["code"]))
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph("主要接口", s["h2"]))
    story.append(endpoint_table(s))
    story.append(Paragraph("创作页右侧的“接口文档”提供当前工作流的参数和调用示例。第三方先读取 GET /v1/workflows/{workflow_id}/schema，再按该 schema 提交参数。", s["body"]))
    story.append(Spacer(1, 4 * mm))

    story.extend([PageBreak(), Paragraph("4. 调用示例与异步任务", s["h1"])])
    story.append(note_box("不要把 API Key 写入公开网页、代码仓库、聊天截图或日志。公网 URL 是互联网入口，应和 API Key 一起妥善保存。", s["body"], SOFT_WARN))
    story.append(Paragraph("运行默认工作流", s["h2"]))
    story.append(code_box('curl -X POST "BASE_URL/" ^\n  -H "Authorization: Bearer API_KEY" ^\n  -H "Content-Type: application/json" ^\n  -d "{\\"prompt\\":\\"一只在窗边晒太阳的猫\\"}"', s["code"]))
    story.append(Paragraph("OpenAI 风格文字接口", s["h2"]))
    story.append(code_box('curl -X POST "BASE_URL/v1/chat/completions" ^\n  -H "Authorization: Bearer API_KEY" ^\n  -H "Content-Type: application/json" ^\n  -d "{\\"model\\":\\"qwen3\\",\\"messages\\":[{\\"role\\":\\"user\\",\\"content\\":\\"写一段产品介绍\\"}]}"', s["code"]))
    story.append(Paragraph("图片生成兼容接口", s["h2"]))
    story.append(code_box('curl -X POST "BASE_URL/api/v3/images/generations" ^\n  -H "Authorization: Bearer API_KEY" ^\n  -H "Content-Type: application/json" ^\n  -d "{\\"prompt\\":\\"电影感城市夜景\\",\\"response_format\\":\\"url\\"}"', s["code"]))
    story.append(Paragraph("异步任务流程", s["h2"]))
    for index, text in enumerate(
        [
            "生成接口返回 task_id；多个入口共用任务队列，可查看 queue_position。",
            "轮询 GET /v1/tasks/{task_id}，直到状态变为完成、失败或取消。需要取消时 POST /v1/tasks/{task_id}/cancel。",
            "从响应读取文件名或 URL。",
            "调用 GET /v1/files/{task_id}/{filename} 下载结果。",
        ],
        1,
    ):
        story.append(Paragraph(f"{index}. {text}", s["step"]))

    story.extend([PageBreak(), Paragraph("5. 常见问题与安全说明", s["h1"])])
    questions = [
        ("客户端能打开，但不能生成", "轻量客户端只负责界面和环境维护。请先下载并安装独立运行环境包，再补齐工作流要求的模型。"),
        ("一键修复拉取失败怎么办", "失败弹窗会显示网络或下载错误，并提供可复制的 GitHub 地址。在项目 Releases 下载 7z 环境包，然后回到客户端选择本地环境包；客户端会使用内置 SHA256 校验。"),
        ("环境检查提示 NVIDIA 驱动过旧", "CUDA 13 运行环境要求使用 R580 或更高驱动。更新驱动后重启电脑，再点击“检查环境”。"),
        ("接口返回 401 或 403", "检查 Authorization 请求头是否使用 `Bearer API_KEY`，并确认复制的是生成接口 Key，而不是本机管理 Key。"),
        ("接口能访问但工作流失败", "在“模型与环境”的工作流列表查看缺失模型和公开参数；确认模型文件完整，默认工作流已选中，ComfyUI 状态正常。"),
        ("登录平台会同步什么", "登录后会向你填写的服务端同步公网 URL、本机 API Key、设备名称、工作流/模型状态和不含提示词的任务进度，供平台调用。提示词与生成结果不在同步数据中；只应登录可信服务。不登录不影响完整本地功能。"),
        ("更新客户端会不会删除模型", "不会。程序、运行环境、模型和用户数据分离。重要模型和输出仍建议定期备份。"),
    ]
    for title, answer in questions:
        story.append(Paragraph(title, s["h2"]))
        story.append(Paragraph(answer, s["body"]))
    story.append(HRFlowable(width="100%", thickness=0.7, color=BORDER, spaceBefore=5 * mm, spaceAfter=4 * mm))
    story.append(Paragraph(f"项目主页：<b>{PROJECT_URL}</b>", s["body"]))
    story.append(Paragraph("建议始终从项目主页或明确授权的镜像获取安装包和环境包；客户端会用内置 SHA256 验证环境包。", s["small"]))
    return story


def build_pdf(output: Path) -> None:
    register_fonts()
    output.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(
        str(output),
        pagesize=A4,
        rightMargin=22 * mm,
        leftMargin=22 * mm,
        topMargin=22 * mm,
        bottomMargin=20 * mm,
        title="LingJingAPI 使用教学与 API 接口说明",
        author="Yaro-lu",
        subject="LingJingAPI 轻量客户端使用教学、环境安装与 API 接口说明",
    )
    document.build(
        build_story(styles()),
        onFirstPage=draw_page_frame,
        onLaterPages=draw_page_frame,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    build_pdf(args.output.resolve())
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
