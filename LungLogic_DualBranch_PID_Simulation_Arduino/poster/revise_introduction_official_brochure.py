"""Revise the LungLogic poster introduction using the official XV LVAS brochure.

The script preserves the existing PowerPoint text formatting and replaces only
the Introduction copy, citation, and 4DMedical visual. The matching HTML,
print PDF, and preview PNG are kept in sync.
"""

from __future__ import annotations

import base64
import re
import shutil
from pathlib import Path

import fitz
from PIL import Image
from pptx import Presentation


HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
BROCHURE = ASSETS / "4DMedical_DOC-5070_XV-LVAS_Info-Sheet.pdf"
BROCHURE_FIGURE = ASSETS / "4DMedical_DOC-5070_regional_ventilation.png"

# The user supplied the prototype files and a preview of the later physical-lung
# variant. Updating both keeps the two current poster variants consistent.
STEMS = (
    "LungLogic_Poster_2026-10-02_prototype_pcb",
    "LungLogic_Poster_2026-10-02_physical_lung_future_work",
)

REVISIONS = {
    "Dynamic lung phantoms provide repeatable breathing inputs for imaging and ventilation research.":
        "Adjustable RR, I:E, tidal volume and regional airflow reproduce patient-like lung conditions for repeatable lung-imaging research.",
    "LungLogic controls airflow in a biomimetic lung model. This stage validates one 250 mL branch before bilateral integration.":
        "Commercial lung models cannot meet 4DMedical's needs. Our programmable airflow system will drive a patient-realistic physical lung developed by the medical team. This stage validates one 250 mL branch.",
    "Regional ventilation imaging.\nApplication context.":
        "XV LVAS ventilation map.",
    "Nominal design values, not measured performance. Target: 250 mL per branch and VT error within ±5%.":
        "Nominal reference values shown; operating parameters are programmable.",
    "Sources: 4DMedical project scope (19 March 2026), pp. 5, 13. Ventilation image: supplied reference.":
        "Source: 4DMedical, XV LVAS Info Sheet, DOC-5070 US V1 (2023), pp. 1-2.",
    "Industry collaborator: 4DMedical | Background: project scope deck, 19 March 2026 | Control: current single-lung system | Experimental results: pending | Bench research prototype.":
        "Industry collaborator: 4DMedical | Background: XV LVAS Info Sheet, DOC-5070 US V1 (2023) | Control: current single-lung system | Experimental results: pending | Bench research prototype.",
}

INTRO_IMAGE_BOX_CM = (2.8, 21.0, 14.64, 12.0061)

PARAMETER_ROWS = (
    ("Respiratory rate", "12–16 bpm; nominal 15 bpm"),
    ("Inhale:exhale time", "1:2; 1.33 s / 2.67 s at 15 bpm"),
    ("Tidal volume", "250 mL per branch"),
    ("Regional airflow", "Independently programmable by branch"),
)


def make_brochure_figure() -> None:
    """Crop the official regional-ventilation panel and pad to the poster box."""
    if not BROCHURE.exists():
        raise FileNotFoundError(BROCHURE)
    document = fitz.open(BROCHURE)
    page = document[1]
    # Exact top-left report panel from page 2: coronal/axial heat maps plus scale.
    clip = fitz.Rect(30, 70, 360, 362)
    pixmap = page.get_pixmap(matrix=fitz.Matrix(4, 4), clip=clip, alpha=False)
    temp = BROCHURE_FIGURE.with_name(BROCHURE_FIGURE.stem + "_crop.png")
    pixmap.save(temp)

    with Image.open(temp) as source:
        source = source.convert("RGB")
        target_ratio = INTRO_IMAGE_BOX_CM[2] / INTRO_IMAGE_BOX_CM[3]
        target_width = round(source.height * target_ratio)
        canvas = Image.new("RGB", (target_width, source.height), "white")
        canvas.paste(source, ((target_width - source.width) // 2, 0))
        canvas.save(BROCHURE_FIGURE, quality=95)
    temp.unlink()


def replace_text_preserving_format(shape, new_text: str) -> None:
    """Change wording while retaining the first run's existing font formatting."""
    text_frame = shape.text_frame
    first_run = text_frame.paragraphs[0].runs[0]
    first_run.text = new_text
    for paragraph in text_frame.paragraphs[1:]:
        paragraph._element.getparent().remove(paragraph._element)
    for run in list(text_frame.paragraphs[0].runs)[1:]:
        run._element.getparent().remove(run._element)


def backup(path: Path) -> None:
    backup_path = path.with_name(path.stem + "_before_intro_revision_2026-10-06" + path.suffix)
    if path.exists() and not backup_path.exists():
        shutil.copy2(path, backup_path)


def update_pptx(stem: str) -> None:
    path = HERE / f"{stem}.pptx"
    backup(path)
    deck = Presentation(path)
    if len(deck.slides) != 1:
        raise ValueError(f"Expected a one-slide poster: {path}")
    slide = deck.slides[0]

    pending = set(REVISIONS)
    for shape in slide.shapes:
        if not shape.has_text_frame:
            continue
        current = shape.text.strip()
        for old, new in REVISIONS.items():
            if current == old:
                replace_text_preserving_format(shape, new)
                pending.discard(old)
                break
            if current == new:
                pending.discard(old)
                break
    if pending:
        raise ValueError(f"Missing PowerPoint text in {stem}: {sorted(pending)}")

    objective = [
        shape for shape in slide.shapes
        if shape.has_text_frame and shape.text.startswith("Commercial lung models")
    ]
    caption = [
        shape for shape in slide.shapes
        if shape.has_text_frame and shape.text.strip() == "XV LVAS ventilation map."
    ]
    if len(objective) != 1 or len(caption) != 1:
        raise ValueError(f"Revised Introduction layout targets not found in {stem}")
    objective[0].height = round(11.2 * 360000)
    caption[0].left = round(1.397 * 360000)
    caption[0].top = round(33.05 * 360000)
    caption[0].width = round(17.5 * 360000)
    caption[0].height = round(1.3 * 360000)

    parameter_tables = [shape for shape in slide.shapes if shape.has_table]
    if len(parameter_tables) != 1:
        raise ValueError(f"Expected one parameter table in {stem}")
    table = parameter_tables[0].table
    if len(table.rows) != len(PARAMETER_ROWS) or len(table.columns) != 2:
        raise ValueError(f"Unexpected parameter table dimensions in {stem}")
    for row, values in zip(table.rows, PARAMETER_ROWS):
        for cell, value in zip(row.cells, values):
            replace_text_preserving_format(cell, value)

    left, top, width, height = INTRO_IMAGE_BOX_CM
    matching_pictures = [
        shape for shape in slide.shapes
        if shape.shape_type == 13
        and abs(shape.left / 360000 - left) < 0.01
        and abs(shape.top / 360000 - top) < 0.01
        and abs(shape.width / 360000 - width) < 0.01
        and abs(shape.height / 360000 - height) < 0.01
    ]
    if len(matching_pictures) != 1:
        raise ValueError(f"Expected one Introduction picture in {stem}")
    old_picture = matching_pictures[0]
    old_picture._element.getparent().remove(old_picture._element)
    slide.shapes.add_picture(
        str(BROCHURE_FIGURE),
        round(left * 360000),
        round(top * 360000),
        round(width * 360000),
        round(height * 360000),
    )
    deck.save(path)


def update_html(stem: str) -> None:
    path = HERE / f"{stem}.html"
    backup(path)
    document = path.read_text(encoding="utf-8")
    for old, new in REVISIONS.items():
        html_old = old.replace("\n", "<br/>")
        html_new = new.replace("\n", "<br/>")
        old_count = document.count(html_old)
        new_count = document.count(html_new)
        if old_count == 1:
            document = document.replace(html_old, html_new, 1)
        elif new_count != 1:
            raise ValueError(f"Expected original or revised HTML text in {stem}: {old}")

    objective_style = "left:19.0000cm;top:22.8000cm;width:21.5000cm;height:8.0000cm;"
    revised_objective_style = "left:19.0000cm;top:22.8000cm;width:21.5000cm;height:11.2000cm;"
    if document.count(objective_style) == 1:
        document = document.replace(objective_style, revised_objective_style, 1)
    elif document.count(revised_objective_style) != 1:
        raise ValueError(f"Objective HTML geometry not found in {stem}")
    caption_style = "left:19.0000cm;top:30.2000cm;width:21.5000cm;height:3.6000cm;"
    revised_caption_style = "left:1.3970cm;top:33.0500cm;width:17.5000cm;height:1.3000cm;"
    if document.count(caption_style) == 1:
        document = document.replace(caption_style, revised_caption_style, 1)
    elif document.count(revised_caption_style) != 1:
        raise ValueError(f"Caption HTML geometry not found in {stem}")

    table_pattern = re.compile(
        r'<table class="item data-table" '
        r'style="left:1\.5494cm;top:47\.4000cm;width:39\.0652cm;height:9\.6520cm;">'
        r'.*?</table>',
        flags=re.DOTALL,
    )
    table_rows = "".join(
        f"<tr><td>{label}</td><td>{value}</td></tr>"
        for label, value in PARAMETER_ROWS
    )
    table_html = (
        '<table class="item data-table" '
        'style="left:1.5494cm;top:47.4000cm;width:39.0652cm;height:9.6520cm;">'
        + table_rows
        + "</table>"
    )
    document, table_count = table_pattern.subn(lambda _: table_html, document, count=1)
    if table_count != 1:
        raise ValueError(f"Parameter table not found in HTML: {stem}")

    encoded = base64.b64encode(BROCHURE_FIGURE.read_bytes()).decode("ascii")
    replacement = (
        '<img alt="4DMedical XV LVAS regional ventilation visualisation" '
        'class="item picture" src="data:image/png;base64,' + encoded + '" '
        'style="left:2.8000cm;top:21.0000cm;width:14.6400cm;height:12.0061cm;"/>'
    )
    pattern = re.compile(
        r'<img [^>]*?src="data:image/(?:png|jpeg);base64,[^"]+" '
        r'style="left:2\.8000cm;top:21\.0000cm;width:14\.6400cm;height:12\.0061cm;"/>'
    )
    document, count = pattern.subn(lambda _: replacement, document, count=1)
    if count != 1:
        raise ValueError(f"Introduction image not found in HTML: {stem}")
    path.write_text(document, encoding="utf-8")


def export_outputs(stem: str) -> None:
    import win32com.client

    pptx = (HERE / f"{stem}.pptx").resolve()
    pdf = (HERE / f"{stem}.pdf").resolve()
    preview = (HERE / f"{stem}_preview.png").resolve()
    app = win32com.client.DispatchEx("PowerPoint.Application")
    app.Visible = True
    try:
        deck = app.Presentations.Open(str(pptx), WithWindow=False)
        deck.SaveAs(str(pdf), 32)
        deck.Slides(1).Export(str(preview), "PNG", 2384, 3370)
        deck.Close()
    finally:
        app.Quit()


def main() -> None:
    make_brochure_figure()
    for stem in STEMS:
        update_pptx(stem)
        update_html(stem)
        export_outputs(stem)
        print(HERE / f"{stem}.pptx")
        print(HERE / f"{stem}.html")
        print(HERE / f"{stem}.pdf")
        print(HERE / f"{stem}_preview.png")


if __name__ == "__main__":
    main()
