#!/usr/bin/env python3
"""Generate Ingabe presentation PDF by re-branding the FarmVibes deck.

Strategy:
1. Render each original page as a high-res raster image (preserving all visuals)
2. Paint over specific text areas that need changing
3. Overlay new Ingabe-branded text

This approach preserves the exact farm photo backgrounds, icons, and layout
while cleanly replacing branded text without affecting adjacent content.
"""

import io
from pathlib import Path

import fitz  # PyMuPDF

SOURCE_PDF = Path("/tmp/nist_reference.pdf")
RENDER_DPI = 150  # Good quality, manageable file size

PW, PH = 960, 540  # Original page size in pts


def hex_to_rgb(h: int) -> tuple:
    return ((h >> 16) / 255, ((h >> 8) & 0xFF) / 255, (h & 0xFF) / 255)


WHITE = hex_to_rgb(0xFCFDFF)
ACCENT = hex_to_rgb(0xBAD80A)  # FarmVibes yellow-green
GREEN_DARK = hex_to_rgb(0x387926)


def rasterize_page(src_doc: fitz.Document, page_idx: int) -> bytes:
    """Render a page to JPEG at target DPI for smaller file size."""
    page = src_doc[page_idx]
    mat = fitz.Matrix(RENDER_DPI / 72, RENDER_DPI / 72)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    return pix.tobytes("jpeg", jpg_quality=88)


def create_page_from_image(dst: fitz.Document, img_bytes: bytes) -> fitz.Page:
    """Create a new page with the rasterized image as full background."""
    page = dst.new_page(width=PW, height=PH)
    page.insert_image(fitz.Rect(0, 0, PW, PH), stream=img_bytes)
    return page


def paint_rect(page: fitz.Page, x0, y0, x1, y1, color):
    """Paint a filled rectangle (to cover old text)."""
    shape = page.new_shape()
    shape.draw_rect(fitz.Rect(x0, y0, x1, y1))
    shape.finish(fill=color, color=color, width=0)
    shape.commit()


def write_text(page: fitz.Page, x, y, text, fontsize=20,
               color=WHITE, bold=False):
    """Write text at a specific position."""
    font = fitz.Font("hebo" if bold else "helv")
    tw = fitz.TextWriter(page.rect)
    tw.append(fitz.Point(x, y), text, fontsize=fontsize, font=font)
    tw.write_text(page, color=color)


def sample_bg_color(src_doc: fitz.Document, page_idx: int,
                    x: float, y: float) -> tuple:
    """Sample the background color at a specific point by rendering a small area."""
    page = src_doc[page_idx]
    # Render a small clip around the point
    clip = fitz.Rect(x - 2, y - 2, x + 2, y + 2)
    pix = page.get_pixmap(clip=clip, alpha=False)
    # Get the center pixel color
    if pix.width > 0 and pix.height > 0:
        cx, cy = pix.width // 2, pix.height // 2
        pixel = pix.pixel(cx, cy)
        return (pixel[0] / 255, pixel[1] / 255, pixel[2] / 255)
    return (0.1, 0.1, 0.15)


def get_span_info(src_doc: fitz.Document, page_idx: int) -> list:
    """Get all text spans with their positions from the original."""
    page = src_doc[page_idx]
    spans = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] == 0:
            for line in b["lines"]:
                for span in line["spans"]:
                    spans.append(span)
    return spans


# ══════════════════════════════════════════════════════════════
# Page-specific modifications
# Each function returns a list of (cover_rect, new_text_params) tuples
# cover_rect: (x0, y0, x1, y1, bg_color) — area to paint over
# new_text_params: (x, y, text, fontsize, color, bold) — new text to draw
# ══════════════════════════════════════════════════════════════

def mods_page1(src, spans):
    """Title slide: replace title and author."""
    mods = []

    # Cover original title and write new one
    # Original: "FarmVibes: AI, Edge, & IoT For Agriculture" at ~[63,318]
    title_span = next((s for s in spans if "FarmVibes" in s["text"]), None)
    if title_span:
        bb = title_span["bbox"]
        bg = sample_bg_color(src, 0, bb[0], bb[1])
        mods.append({
            "cover": (bb[0] - 2, bb[1] - 2, bb[2] + 5, bb[3] + 2, bg),
            "text": (bb[0], bb[3] - 4, "Ingabe: AI-Powered GIS for Agriculture",
                     title_span["size"], WHITE, True)
        })

    # Cover author name
    author_span = next((s for s in spans if "Ranveer" in s["text"]), None)
    if author_span:
        bb = author_span["bbox"]
        bg = sample_bg_color(src, 0, bb[0], bb[1])
        mods.append({
            "cover": (bb[0] - 2, bb[1] - 2, bb[2] + 60, bb[3] + 2, bg),
            "text": (bb[0], bb[3] - 3, "NozaLabs  |  gis.nozalabs.rw",
                     author_span["size"], WHITE, False)
        })

    # Cover Microsoft logos (top-left and bottom-left)
    mods.append({"cover": (5, 28, 115, 56, sample_bg_color(src, 0, 60, 40)), "text": None})
    mods.append({"cover": (3, 513, 90, 538, sample_bg_color(src, 0, 40, 525)), "text": None})

    return mods


def mods_page2(src, spans):
    """Food security stats — just remove Microsoft logo."""
    return [
        {"cover": (3, 513, 90, 538, sample_bg_color(src, 1, 40, 525)), "text": None},
    ]


def mods_page4(src, spans):
    """Copyright + Microsoft references."""
    mods = []
    for s in spans:
        if "Microsoft" in s["text"] and ("Copyright" in s["text"] or "©" in s["text"]):
            bb = s["bbox"]
            bg = sample_bg_color(src, 3, bb[0], bb[1])
            mods.append({
                "cover": (bb[0] - 2, bb[1] - 1, bb[2] + 30, bb[3] + 1, bg),
                "text": (bb[0], bb[3] - 2, "© NozaLabs. All rights reserved.",
                         s["size"], hex_to_rgb(s["color"]), False)
            })
    # Logo at bottom-left
    mods.append({"cover": (3, 513, 90, 538, sample_bg_color(src, 3, 40, 525)), "text": None})
    return mods


def mods_page5(src, spans):
    """'According to USDA' -> 'Across Africa, the' — need to cover whole first line."""
    mods = []
    # Original first line spans: "According to USDA," + "high cost of manual"
    # Cover just "According to USDA," and replace
    usda_span = next((s for s in spans if "USDA" in s["text"]), None)
    if usda_span:
        bb = usda_span["bbox"]
        bg = sample_bg_color(src, 4, bb[0], bb[1])
        mods.append({
            "cover": (bb[0] - 2, bb[1] - 2, bb[2] - 1, bb[3] + 2, bg),
            "text": (bb[0], bb[3] - 4, "Across Africa, the",
                     usda_span["size"], WHITE, True)
        })

    # Copyright
    for s in spans:
        if "Microsoft" in s["text"]:
            bb = s["bbox"]
            bg = sample_bg_color(src, 4, bb[0], bb[1])
            mods.append({
                "cover": (bb[0] - 2, bb[1] - 1, bb[2] + 30, bb[3] + 1, bg),
                "text": (bb[0], bb[3] - 2, "© NozaLabs. All rights reserved.",
                         s["size"], hex_to_rgb(s["color"]), False)
            })
    return mods


def mods_page6(src, spans):
    """Connectivity challenge — copyright only."""
    return _copyright_mods(src, 5, spans)


def mods_page7(src, spans):
    """White space -> satellite data."""
    mods = _copyright_mods(src, 6, spans)
    for s in spans:
        if s["text"].strip() == "A solution in white space":
            bb = s["bbox"]
            bg = sample_bg_color(src, 6, bb[0], bb[1])
            mods.append({
                "cover": (bb[0] - 2, bb[1] - 2, bb[2] + 10, bb[3] + 2, bg),
                "text": (bb[0], bb[3] - 3, "A solution with satellite data",
                         s["size"], hex_to_rgb(s["color"]), True)
            })
        if s["text"].strip() == "Increasing wireless reach":
            bb = s["bbox"]
            bg = sample_bg_color(src, 6, bb[0], bb[1])
            mods.append({
                "cover": (bb[0] - 2, bb[1] - 2, bb[2] + 5, bb[3] + 2, bg),
                "text": (bb[0], bb[3] - 3, "Increasing data coverage",
                         s["size"], hex_to_rgb(s["color"]), False)
            })
        if s["text"].strip() == "with TV White Space":
            bb = s["bbox"]
            bg = sample_bg_color(src, 6, bb[0], bb[1])
            mods.append({
                "cover": (bb[0] - 2, bb[1] - 2, bb[2] + 5, bb[3] + 2, bg),
                "text": (bb[0], bb[3] - 3, "with Sentinel-2 imagery",
                         s["size"], hex_to_rgb(s["color"]), False)
            })
    return mods


def mods_page8(src, spans):
    """Sparse sensors."""
    mods = _copyright_mods(src, 7, spans)
    for s in spans:
        if "Sparse sensor" in s["text"]:
            bb = s["bbox"]
            bg = sample_bg_color(src, 7, bb[0], bb[1])
            mods.append({
                "cover": (bb[0] - 2, bb[1] - 2, bb[2] + 10, bb[3] + 2, bg),
                "text": (bb[0], bb[3] - 3, "Challenge: Limited ground sensor coverage",
                         s["size"], hex_to_rgb(s["color"]), True)
            })
    return mods


def mods_page9(src, spans):
    """Aerial imagery + AI — keep as-is, just copyright."""
    return _copyright_mods(src, 8, spans)


def mods_page10(src, spans):
    """Edge compute -> Cloud compute."""
    mods = _copyright_mods(src, 9, spans)
    for s in spans:
        if s["text"].strip() == "Edge Compute in the Farm":
            bb = s["bbox"]
            bg = sample_bg_color(src, 9, bb[0], bb[1])
            mods.append({
                "cover": (bb[0] - 2, bb[1] - 2, bb[2] + 10, bb[3] + 2, bg),
                "text": (bb[0], bb[3] - 3, "Cloud Computing for the Farm",
                         s["size"], hex_to_rgb(s["color"]), True)
            })
        if "Azure IoT Edge" in s["text"]:
            bb = s["bbox"]
            bg = sample_bg_color(src, 9, bb[0], bb[1])
            mods.append({
                "cover": (bb[0] - 2, bb[1] - 2, bb[2] + 10, bb[3] + 2, bg),
                "text": (bb[0], bb[3] - 3, "Ingabe Cloud Platform",
                         s["size"], hex_to_rgb(s["color"]), False)
            })
    return mods


def mods_page11(src, spans):
    """IoT architecture diagram."""
    mods = _copyright_mods(src, 10, spans)
    for s in spans:
        if s["text"].strip() == "IoT Edge":
            bb = s["bbox"]
            bg = sample_bg_color(src, 10, bb[0], bb[1])
            mods.append({
                "cover": (bb[0] - 2, bb[1] - 2, bb[2] + 10, bb[3] + 2, bg),
                "text": (bb[0], bb[3] - 3, "Ingabe Platform",
                         s["size"], hex_to_rgb(s["color"]), True)
            })
    return mods


def mods_page12(src, spans):
    """Deployment — light background. Replace locations and brand names."""
    mods = []
    for s in spans:
        txt = s["text"].strip()
        bb = s["bbox"]
        bg = (1.0, 1.0, 1.0)  # white background page

        if txt == "Deployments in several locations":
            mods.append({
                "cover": (bb[0] - 2, bb[1] - 1, bb[2] + 5, bb[3] + 1, bg),
                "text": (bb[0], bb[3] - 3, "Deployed across Rwanda",
                         s["size"], hex_to_rgb(s["color"]), False)
            })
        elif txt == "including WA, CA, NY":
            mods.append({
                "cover": (bb[0] - 2, bb[1] - 1, bb[2] + 5, bb[3] + 1, bg),
                "text": (bb[0], bb[3] - 3, "covering all 30 districts",
                         s["size"], hex_to_rgb(s["color"]), False)
            })
        elif "FarmBeats" in txt:
            new_txt = txt.replace("FarmBeats", "Ingabe")
            mods.append({
                "cover": (bb[0] - 2, bb[1] - 1, bb[2] + 20, bb[3] + 1, bg),
                "text": (bb[0], bb[3] - 2, new_txt,
                         s["size"], hex_to_rgb(s["color"]), False)
            })
        elif "Azure" in txt:
            new_txt = txt.replace("Azure", "Ingabe Cloud")
            mods.append({
                "cover": (bb[0] - 2, bb[1] - 1, bb[2] + 30, bb[3] + 1, bg),
                "text": (bb[0], bb[3] - 3, new_txt,
                         s["size"], hex_to_rgb(s["color"]), False)
            })
        elif "Microsoft" in txt:
            mods.append({
                "cover": (bb[0] - 2, bb[1] - 1, bb[2] + 30, bb[3] + 1, bg),
                "text": (bb[0], bb[3] - 2, "© NozaLabs. All rights reserved.",
                         s["size"], hex_to_rgb(s["color"]), False)
            })
    return mods


def mods_page13(src, spans):
    """Micro-climate forecasting — replace FarmBeats reference."""
    mods = _copyright_mods(src, 12, spans)
    for s in spans:
        if "FarmBeats" in s["text"]:
            bb = s["bbox"]
            bg = sample_bg_color(src, 12, bb[0], bb[1])
            new_txt = s["text"].replace("FarmBeats", "Ingabe")
            mods.append({
                "cover": (bb[0] - 2, bb[1] - 1, bb[2] + 20, bb[3] + 1, bg),
                "text": (bb[0], bb[3] - 2, new_txt,
                         s["size"], hex_to_rgb(s["color"]), False)
            })
    return mods


def mods_page14(src, spans):
    return _copyright_mods(src, 13, spans)


def mods_page15(src, spans):
    """Panorama -> satellite composite."""
    mods = _copyright_mods(src, 14, spans)
    for s in spans:
        if "Panorama Generation" in s["text"]:
            bb = s["bbox"]
            bg = sample_bg_color(src, 14, bb[0], bb[1])
            mods.append({
                "cover": (bb[0] - 2, bb[1] - 2, bb[2] + 10, bb[3] + 2, bg),
                "text": (bb[0], bb[3] - 3, "Satellite Composite Generation",
                         s["size"], hex_to_rgb(s["color"]), True)
            })
    return mods


def mods_page16(src, spans):
    """Moisture map."""
    mods = _copyright_mods(src, 15, spans)
    # Title "Precision Map: Moisture" stays mostly the same
    return mods


def mods_page17(src, spans):
    """pH map."""
    return _copyright_mods(src, 16, spans)


def mods_page18(src, spans):
    """Cow-shed monitor — keep as-is."""
    return []  # No Microsoft branding on this page


def mods_page19(src, spans):
    """Education — TechSpark -> NozaLabs training."""
    mods = _copyright_mods(src, 18, spans)
    replacements = {
        "The Microsoft TechSpark initiative is": "The NozaLabs training initiative is",
        "classroom with": "Rwanda with",
        "FarmBeats student kits.": "Ingabe training programs.",
        "Future Farmers of America + FarmBeats + FarmVibes":
            "Rwandan Farmers + Agronomists + Ingabe",
    }
    for s in spans:
        txt = s["text"].strip()
        for old, new in replacements.items():
            if txt == old or old in txt:
                bb = s["bbox"]
                bg = sample_bg_color(src, 18, bb[0], bb[1])
                full_new = txt.replace(old, new) if old in txt else new
                mods.append({
                    "cover": (bb[0] - 2, bb[1] - 1, bb[2] + 30, bb[3] + 1, bg),
                    "text": (bb[0], bb[3] - 2, full_new,
                             s["size"], hex_to_rgb(s["color"]),
                             "Bold" in s.get("font", "") or "Semibold" in s.get("font", ""))
                })
                break
    return mods


def mods_page20(src, spans):
    """Affordable sensing — light background."""
    mods = []
    bg = (1.0, 1.0, 1.0)
    for s in spans:
        txt = s["text"].strip()
        bb = s["bbox"]
        if txt == "Affordable sensing":
            mods.append({
                "cover": (bb[0] - 2, bb[1] - 1, bb[2] + 20, bb[3] + 1, bg),
                "text": (bb[0], bb[3] - 3, "Free satellite sensing",
                         s["size"], hex_to_rgb(s["color"]), True)
            })
        elif "Ranveer Chandra" in txt:
            new_txt = txt.replace("Ranveer Chandra", "NozaLabs Team")
            mods.append({
                "cover": (bb[0] - 2, bb[1] - 1, bb[2] + 20, bb[3] + 1, bg),
                "text": (bb[0], bb[3] - 2, new_txt,
                         s["size"], hex_to_rgb(s["color"]), False)
            })
    return mods


def mods_page21(src, spans):
    """Satellite edge — keep as-is, universal content."""
    return []


def mods_page22(src, spans):
    """Change detection — keep as-is."""
    return []


def mods_page23(src, spans):
    """Soil carbon modeling."""
    return _copyright_mods(src, 22, spans)


def mods_page24(src, spans):
    """Closing slide."""
    mods = []
    for s in spans:
        txt = s["text"].strip()
        bb = s["bbox"]
        bg = sample_bg_color(src, 23, bb[0], bb[1])
        if "Microsoft" in txt:
            mods.append({
                "cover": (bb[0] - 2, bb[1] - 1, bb[2] + 30, bb[3] + 1, bg),
                "text": (bb[0], bb[3] - 2, "© NozaLabs",
                         s["size"], hex_to_rgb(s["color"]), False)
            })
        if "@ranveerchandra" in txt:
            mods.append({
                "cover": (bb[0] - 2, bb[1] - 1, bb[2] + 30, bb[3] + 1, bg),
                "text": (bb[0], bb[3] - 2, "gis.nozalabs.rw",
                         s["size"], hex_to_rgb(s["color"]), False)
            })
    # Cover the large Microsoft logo image — sample color from nearby dark area
    mods.append({
        "cover": (35, 225, 225, 280, sample_bg_color(src, 23, 30, 300)),
        "text": None
    })
    return mods


def _copyright_mods(src, page_idx, spans):
    """Generate mods to replace copyright text."""
    mods = []
    for s in spans:
        txt = s["text"].strip()
        if "Microsoft" in txt and ("Copyright" in txt or "©" in txt):
            bb = s["bbox"]
            bg = sample_bg_color(src, page_idx, bb[0], bb[1])
            mods.append({
                "cover": (bb[0] - 2, bb[1] - 1, bb[2] + 35, bb[3] + 1, bg),
                "text": (bb[0], bb[3] - 2, "© NozaLabs. All rights reserved.",
                         s["size"], hex_to_rgb(s["color"]), False)
            })
    return mods


# ══════════════════════════════════════════════════════════════

PAGE_MOD_FUNCS = [
    mods_page1, mods_page2, None, mods_page4, mods_page5,
    mods_page6, mods_page7, mods_page8, mods_page9, mods_page10,
    mods_page11, mods_page12, mods_page13, mods_page14, mods_page15,
    mods_page16, mods_page17, mods_page18, mods_page19, mods_page20,
    mods_page21, mods_page22, mods_page23, mods_page24,
]


def create_branded_presentation():
    if not SOURCE_PDF.exists():
        print(f"ERROR: Source PDF not found at {SOURCE_PDF}")
        return

    src = fitz.open(str(SOURCE_PDF))
    dst = fitz.open()

    print(f"Source: {len(src)} pages, {src[0].rect.width}x{src[0].rect.height} pts")

    for pg in range(len(src)):
        # Step 1: Rasterize the original page
        img_bytes = rasterize_page(src, pg)

        # Step 2: Create new page with rasterized background
        page = create_page_from_image(dst, img_bytes)

        # Step 3: Apply page-specific modifications
        if pg < len(PAGE_MOD_FUNCS) and PAGE_MOD_FUNCS[pg]:
            spans = get_span_info(src, pg)
            mods = PAGE_MOD_FUNCS[pg](src, spans)

            for mod in mods:
                # Paint cover rectangle
                if mod["cover"]:
                    x0, y0, x1, y1, bg = mod["cover"]
                    paint_rect(page, x0, y0, x1, y1, bg)

                # Write new text
                if mod.get("text"):
                    x, y, text, fontsize, color, bold = mod["text"]
                    write_text(page, x, y, text, fontsize=fontsize,
                               color=color, bold=bold)

        print(f"  Page {pg + 1}: done")

    out_path = Path(__file__).parent.parent / "docs" / "Ingabe_Presentation.pdf"
    dst.save(str(out_path), garbage=4, deflate=True)
    dst.close()
    src.close()
    print(f"\nPDF saved to: {out_path}")
    print(f"File size: {out_path.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    create_branded_presentation()
