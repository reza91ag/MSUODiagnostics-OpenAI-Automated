"""Dependency-free, vector PDF export of MSUO Observatory measurements.

Produces an A4 landscape research report from the same computed data as the
LaTeX export. No remote services, templates, or AI-generated narrative.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from xml.etree import ElementTree

PAGE_WIDTH, PAGE_HEIGHT = 842, 595  # A4 landscape, points
LEFT, RIGHT = 42, 800
NAVY = (0.090, 0.200, 0.310)
BLUE = (0.225, 0.471, 0.659)
LIGHT_BLUE = (0.643, 0.741, 0.816)
TEAL = (0.075, 0.545, 0.510)
MUTED = (0.39, 0.47, 0.54)
PALE = (0.952, 0.970, 0.983)
LINE = (0.86, 0.90, 0.93)
WHITE = (1, 1, 1)


def _ascii(value):
    text = unicodedata.normalize("NFKD", str(value))
    return text.encode("ascii", "replace").decode("ascii")


def _pdf_text(value):
    return _ascii(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _short(value, width, size):
    """Keep dynamic labels within their allocated table cells."""
    text = _ascii(value).replace("\r", " ").replace("\n", " ")
    # Helvetica average glyph width is ~0.5 em. Be conservative.
    max_chars = max(1, int(width / (size * 0.56)))
    return text if len(text) <= max_chars else text[:max(1, max_chars - 3)] + "..."


def _fmt(value):
    return "--" if value is None else f"{value:.2f}"


def _rgb(rgb):
    return " ".join(f"{v:.3f}" for v in rgb)


class _Pdf:
    def __init__(self):
        self.pages = []
        self.current = []
        self.page_number = 0
        self.y = 0

    def raw(self, command):
        self.current.append(command)

    def fill(self, rgb):
        self.raw(f"{_rgb(rgb)} rg")

    def stroke(self, rgb):
        self.raw(f"{_rgb(rgb)} RG")

    def rect(self, x, y, w, h, color):
        self.fill(color)
        self.raw(f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re f")

    def line(self, x1, y1, x2, y2, color=LINE, width=0.6):
        self.stroke(color)
        self.raw(f"{width:.2f} w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")

    def text(self, x, y, value, size=9, bold=False, color=NAVY, max_width=None):
        if max_width is not None:
            value = _short(value, max_width, size)
        self.fill(color)
        font = "F2" if bold else "F1"
        self.raw(f"BT /{font} {size:.2f} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm ({_pdf_text(value)}) Tj ET")

    def logo(self, x, y, size=37):
        """Draw the supplied MSUO SVG as native PDF paths (M/L/Z only)."""
        logo = Path(__file__).resolve().parent / "web" / "msuo-logo.svg"
        if not logo.is_file():
            return
        try:
            root = ElementTree.parse(logo).getroot()
            path = next(node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "path")
            d = path.attrib["d"]
            tokens = re.findall(r"[MLZmlz]|[-+]?(?:\d*\.\d+|\d+)", d)
            scale = size / 283
            self.raw("q")
            self.fill(NAVY)
            self.raw(f"{scale:.6f} 0 0 {-scale:.6f} {x:.2f} {y+size:.2f} cm")
            i = 0
            while i < len(tokens):
                token = tokens[i]
                if token in ("M", "L", "m", "l"):
                    command = "m" if token in ("M", "m") else "l"
                    i += 1
                    while i + 1 < len(tokens) and re.fullmatch(r"[-+\d.]", tokens[i][0]):
                        self.raw(f"{float(tokens[i]):.2f} {float(tokens[i+1]):.2f} {command}")
                        i += 2
                        command = "l"
                elif token in ("Z", "z"):
                    self.raw("h")
                    i += 1
                else:
                    i += 1
            self.raw("f* Q")
        except (OSError, ValueError, StopIteration, ElementTree.ParseError):
            pass

    def new_page(self):
        if self.current:
            self.pages.append("\n".join(self.current).encode("ascii"))
        self.current = []
        self.page_number += 1
        self.logo(LEFT, 536, 37)
        self.text(90, 557, "MSUO Diagnostics Dashboard", 18, True)
        self.text(91, 541, "LIVE EXECUTION OBSERVATORY  /  WATCHDOG MEASUREMENTS", 8, color=MUTED)
        self.line(LEFT, 527, RIGHT, 527)
        self.line(LEFT, 38, RIGHT, 38)
        self.text(LEFT, 24, "MSUO Diagnostics Dashboard", 8, color=MUTED)
        self.text(748, 24, f"Page {self.page_number}", 8, color=MUTED)
        self.y = 508

    def section(self, title):
        if self.y < 93:
            self.new_page()
        self.text(LEFT, self.y, title, 11, True)
        self.y -= 21

    def table(self, title, headers, widths, rows):
        assert sum(widths) <= RIGHT - LEFT
        if self.y < 130:
            self.new_page()
        self.section(title)
        height = 22
        x = LEFT

        def header():
            self.rect(LEFT, self.y-height+5, sum(widths), height, NAVY)
            xx = LEFT
            for heading, width in zip(headers, widths):
                self.text(xx + 7, self.y-10, heading, 8, True, WHITE, width - 13)
                xx += width
            self.y -= height - 2

        header()
        for index, row in enumerate(rows):
            if self.y - height < 53:
                self.new_page()
                self.section(title + " (continued)")
                header()
            if index % 2 == 0:
                self.rect(LEFT, self.y-height+4, sum(widths), height, PALE)
            xx = LEFT
            for item, width in zip(row, widths):
                self.text(xx+7, self.y-10, item, 8.3, max_width=width-13)
                xx += width
            self.line(LEFT, self.y-height+4, LEFT+sum(widths), self.y-height+4, LINE, 0.35)
            self.y -= height
        self.y -= 16

    def bytes(self):
        if self.current:
            self.pages.append("\n".join(self.current).encode("ascii"))
            self.current = []
        # Reserve catalog, pages, and font objects for forward references.
        objects = [None, None, None, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                   b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"]
        page_refs = []
        for content in self.pages:
            stream_id = len(objects) + 1
            objects.append(f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream")
            page_id = len(objects) + 1
            page_refs.append(page_id)
            objects.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
                            f"/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> "
                            f"/Contents {stream_id} 0 R >>").encode())
        objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
        objects[1] = (f"<< /Type /Pages /Kids [{' '.join(f'{i} 0 R' for i in page_refs)}] "
                      f"/Count {len(page_refs)} >>").encode()
        objects[2] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
        # Objects are indexed 1-based; index 3 is unused but harmless.
        result = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for number, payload in enumerate(objects, 1):
            offsets.append(len(result))
            result.extend(f"{number} 0 obj\n".encode())
            result.extend(payload)
            result.extend(b"\nendobj\n")
        startxref = len(result)
        result.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
        for offset in offsets[1:]:
            result.extend(f"{offset:010d} 00000 n \n".encode())
        result.extend((f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\n"
                       f"startxref\n{startxref}\n%%EOF\n").encode())
        return bytes(result)


def _chart(pdf, groups):
    pdf.section("Lifecycle latency (ms)")
    x0, x1 = 255, 747
    available = [v for group in groups for v in (group.get("latest_ms"), group.get("mean_ms")) if v is not None]
    max_value = max(available, default=1)
    for i, group in enumerate(groups):
        y = pdf.y - 15 - i * 34
        pdf.text(LEFT+2, y+6, group["label"], 9, max_width=190)
        for key, offset, color in (("latest_ms", 8, BLUE), ("mean_ms", -5, LIGHT_BLUE)):
            value = group.get(key)
            if value is None:
                continue
            width = max(1, (x1-x0) * value/max_value)
            pdf.rect(x0, y+offset, width, 9, color)
            label_x = min(x0+width+7, 757)
            pdf.text(label_x, y+offset+1, _fmt(value), 7.4, max_width=44)
    pdf.y -= len(groups)*34+21
    pdf.text(x0, pdf.y+10, "Latest", 8, color=BLUE)
    pdf.rect(x0+32, pdf.y+9, 14, 7, BLUE)
    pdf.text(x0+64, pdf.y+10, "Mean", 8, color=MUTED)
    pdf.rect(x0+95, pdf.y+9, 14, 7, LIGHT_BLUE)
    pdf.y -= 20


def render_pdf(data):
    """Render a ready-to-download PDF with vector chart and measurement tables."""
    pdf = _Pdf()
    pdf.new_page()
    pdf.text(LEFT, pdf.y, "This report has been generated by MSUO Diagnostics Dashboard.", 9, color=MUTED)
    pdf.y -= 26
    operation = data["selected_operation"]
    selection = [
        ("Operation", "All operations" if operation == "all" else operation),
        ("Window", "All requests" if data["window"] == 0 else f"Last {data['window']} per configuration"),
        ("Total lifecycles", str(data["total_requests"])),
        ("Generated (UTC)", data["generated_utc"]),
    ]
    pdf.rect(LEFT, pdf.y-30, RIGHT-LEFT, 44, PALE)
    for i, (key, value) in enumerate(selection):
        x = LEFT + 12 + i*188
        pdf.text(x, pdf.y, key.upper(), 7.2, True, MUTED)
        pdf.text(x, pdf.y-16, value, 9.2, max_width=172)
    pdf.y -= 54
    _chart(pdf, data["groups"])
    latency = [(g["label"], str(g["n"]), _fmt(g["latest_ms"]),
                _fmt(g["mean_ms"]), _fmt(g["p95_ms"])) for g in data["groups"]]
    pdf.table("Lifecycle latency by configuration (ms)",
              ["Configuration", "n", "Latest", "Mean", "P95"],
              [290, 65, 132, 132, 139], latency)
    dispersion = [(g["label"], _fmt(g["median_ms"]), _fmt(g["stddev_ms"]),
                   _fmt(g["divergence_ms"]), _fmt(g["divergence_pct"])) for g in data["groups"]]
    pdf.table("Latency dispersion and divergence",
              ["Configuration", "Median (ms)", "SD (ms)", "Delta (ms)", "Delta (%)"],
              [290, 117, 117, 117, 117], dispersion)
    sources = [(s["name"], s.get("persistence") or "--", str(s["files"]), str(s["requests"]))
               for s in data["sources"]]
    if sources:
        pdf.table("Measurement sources", ["Source", "Persistence", "Files", "Lifecycles"],
                  [360, 188, 105, 105], sources)
    recent = [
        (r.get("end_utc", ""), r.get("source", ""), r.get("operation", ""),
         r.get("persistence", ""), _fmt(r.get("duration_ms")), r.get("startup", ""))
        for r in data.get("recent", [])[:30]
    ]
    if recent:
        pdf.table("Recent completed lifecycles", ["Completed (UTC)", "Source", "Operation", "Persistence", "Duration (ms)", "Startup"],
                  [173, 155, 154, 100, 108, 68], recent)
    return pdf.bytes()