"""合成测试夹具生成器。

**这里的全部内容都是合成的虚构数据，不是任何真实县志、丛书或文物档案。**
文件里带有 :data:`SYNTHETIC_MARK` 标识，方便在报告与检索中一眼分辨。

生成物：

* ``text_pdf``       可选取文字的三页 PDF（用于文字检索、跨换行匹配）
* ``scan_pdf``       只有图片、没有文字层的扫描版 PDF
* ``header_scan_pdf``图片 + 仅有一个可选页码的文字层（考验 OCR 判定）
* ``mixed_pdf``      文字页 + 扫描页混合
* ``docx``           按文档顺序排列的段落与表格
"""

from __future__ import annotations

import io
from pathlib import Path

from litworkbench.errors import LitError

#: 所有合成资料都会写入该标识
SYNTHETIC_MARK = "【合成测试数据·非真实县志】"

CJK_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)

IMAGE_PAGES = [
    (
        "合成扫描样例",
        ["白塔为县级文物保护单位。", "道光十四年建，砖石结构。", SYNTHETIC_MARK],
    ),
    (
        "合成扫描样例（碑记）",
        ["重修白塔碑记：塔高七丈。", "光绪年间立石。", SYNTHETIC_MARK],
    ),
]


def find_cjk_font() -> Path | None:
    """找一个可用的中文字体；找不到返回 ``None``（调用方应明确报告跳过）。"""
    for candidate in CJK_FONT_CANDIDATES:
        path = Path(candidate)
        if path.exists():
            return path
    return None


def make_fixtures(dest: Path | str, font: Path | None = None) -> dict[str, Path]:
    """生成全部合成夹具，返回 ``{名称: 路径}``。"""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    out = Path(dest).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    font_path = font or find_cjk_font()

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

    text_pdf = out / "合成样例_文字版.pdf"
    _write_text_pdf(text_pdf, A4)

    if font_path is None:
        raise LitError(
            "未找到可用的中文字体，无法生成图片型 PDF 夹具。"
            "请安装中文字体或使用 --font 指定 .ttf/.ttc 路径。"
        )

    scan_pdf = out / "合成样例_扫描版.pdf"
    _write_image_pdf(scan_pdf, A4, IMAGE_PAGES[0], font_path)

    header_scan = out / "合成样例_仅页码扫描版.pdf"
    _write_image_pdf(header_scan, A4, IMAGE_PAGES[1], font_path, page_label="3")

    mixed_pdf = out / "合成样例_混合版.pdf"
    _write_mixed_pdf(mixed_pdf, A4, font_path)

    docx_path = out / "合成样例_段落与表格.docx"
    _write_docx(docx_path)

    return {
        "dir": out,
        "text_pdf": text_pdf,
        "scan_pdf": scan_pdf,
        "header_scan_pdf": header_scan,
        "mixed_pdf": mixed_pdf,
        "docx": docx_path,
        "font": font_path,
    }


# ----------------------------------------------------------------- PDF


def _page_texts() -> list[tuple[str, list[str]]]:
    return [
        (
            "青川县志（虚构合成样例）",
            [
                SYNTHETIC_MARK,
                "本文件为软件测试用合成数据，不对应任何真实地方志。",
                "卷一 文物：白塔位于县城北侧，为县级文物保护单位。",
                "白塔旁另存残碑一通，文字漫漶。",
            ],
        ),
        (
            "卷二 古迹",
            [
                SYNTHETIC_MARK,
                "白塔始建于清初，屡经修葺。",
                "塔基用条石砌成，塔身砖雕莲瓣纹。",
            ],
        ),
        (
            "卷三 碑刻",
            [
                SYNTHETIC_MARK,
                # 刻意换行，用于验证跨行中文检索
                "白塔建于道光",
                "十四年，为县内古迹。",
                "碑文另载：塔顶旧有铁葫芦，锈蚀缺失。",
            ],
        ),
    ]


def _write_text_pdf(path: Path, pagesize: tuple[float, float]) -> None:
    from reportlab.pdfgen import canvas

    pdf = canvas.Canvas(str(path), pagesize=pagesize)
    for title, lines in _page_texts():
        y = pagesize[1] - 80
        pdf.setFont("STSong-Light", 16)
        pdf.drawString(70, y, title)
        y -= 34
        pdf.setFont("STSong-Light", 12)
        for line in lines:
            pdf.drawString(70, y, line)
            y -= 22
        pdf.showPage()
    pdf.save()


def _render_page_image(lines: list[str], font_path: Path, width: int = 1240,
                       height: int = 1754) -> "object":
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(font_path), 46)
    y = 120
    for line in lines:
        draw.text((90, y), line, font=font, fill="black")
        y += 90
    return image


def _write_image_pdf(path: Path, pagesize: tuple[float, float], page: tuple[str, list[str]],
                     font_path: Path, page_label: str | None = None) -> None:
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    title, lines = page
    image = _render_page_image([title] + lines, font_path)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)

    pdf = canvas.Canvas(str(path), pagesize=pagesize)
    pdf.drawImage(ImageReader(buffer), 0, 0, width=pagesize[0], height=pagesize[1])
    if page_label is not None:
        # 扫描件上常见的页码：唯一可选取的文字层，正文完全没有文字层
        pdf.setFont("STSong-Light", 10)
        pdf.drawCentredString(pagesize[0] / 2, 40, f"第 {page_label} 页")
    pdf.showPage()
    pdf.save()
    image.close()


def _write_mixed_pdf(path: Path, pagesize: tuple[float, float], font_path: Path) -> None:
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    pdf = canvas.Canvas(str(path), pagesize=pagesize)
    # 第 1 页：正常文字页
    pdf.setFont("STSong-Light", 16)
    pdf.drawString(70, pagesize[1] - 90, "混合样例（虚构）")
    pdf.setFont("STSong-Light", 12)
    pdf.drawString(70, pagesize[1] - 130, SYNTHETIC_MARK)
    pdf.drawString(70, pagesize[1] - 160, "白塔见于卷二古迹门。")
    pdf.showPage()

    # 第 2 页：只有图片
    title, lines = IMAGE_PAGES[1]
    image = _render_page_image([title] + lines, font_path)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    pdf.drawImage(ImageReader(buffer), 0, 0, width=pagesize[0], height=pagesize[1])
    pdf.showPage()
    pdf.save()
    image.close()


# ---------------------------------------------------------------- DOCX


def _write_docx(path: Path) -> None:
    import docx

    document = docx.Document()
    document.add_heading("第一章 文物概况（合成样例）", level=1)
    document.add_paragraph(f"{SYNTHETIC_MARK}本章为测试数据。")
    table = document.add_table(rows=3, cols=3)
    table.style = "Table Grid"
    data = [
        ["名称", "年代", "备注"],
        ["白塔", "乾隆", "砖塔"],
        ["白塔碑记", "乾隆", "残石"],
    ]
    for row, values in zip(table.rows, data):
        for cell, value in zip(row.cells, values):
            cell.text = value
    document.add_paragraph("白塔位于县城北侧，为县级文物保护单位。")
    document.add_paragraph("以上内容均为软件测试用合成数据。")
    document.save(str(path))