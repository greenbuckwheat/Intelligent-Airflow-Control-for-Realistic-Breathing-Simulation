"""Apply the Fig. 1 spacing request to the user's landscape poster only."""
import shutil
import re
import subprocess
import tempfile

from bs4 import BeautifulSoup
import fitz
from update_physical_figures_20261007 import HERE, position

STEM = "LungLogic_Revised_Title_Poster_2026-10-07"


def main():
    html = HERE / f"{STEM}.html"
    backup = HERE / f"{STEM}_before_fig1.html"
    if not backup.exists():
        shutil.copy2(html, backup)
    soup = BeautifulSoup(html.read_text(encoding="utf-8"), "html.parser")
    original_elements = [str(e) for e in soup.select("article.poster > *")]
    image = soup.find("img", alt="4DMedical XV LVAS regional ventilation visualisation")
    boxes = soup.select("div.text")
    caption = next(e for e in boxes if e.get_text().startswith("Fig. 1."))
    heading = next(e for e in boxes if e.get_text().startswith("1.2 Project objective"))
    objectives = next(e for e in boxes if e.find("ul"))
    edited = {str(e) for e in (image, caption, heading, objectives)}
    position(image, top=21.8, width=12.2, height=12.2 * 12.0061 / 14.64)
    position(caption, top=32.0, width=17.4, height=1.3)
    caption["style"] += "white-space:nowrap;"
    position(heading, left=15.0, width=22.7)
    position(objectives, left=15.0, width=22.7)
    # Restore the supplied poster's inherited body line-height (1.08).
    objectives["style"] = re.sub(r"line-height:[^;]+;", "", objectives["style"])
    for point in objectives.select("li"):
        point["style"] = "margin:0 0 .18cm 0;"
        if point.get_text() == "Support repeatable breathing cycles.":
            point["style"] += "white-space:nowrap;"
    updated_elements = {str(e) for e in soup.select("article.poster > *")}
    assert all(e in updated_elements for e in original_elements if e not in edited)
    html.write_text(str(soup), encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="lunglogic_landscape_") as profile:
        subprocess.run([
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            "--headless", "--disable-gpu", "--no-pdf-header-footer", "--no-first-run",
            "--disable-extensions", f"--user-data-dir={profile}",
            f"--print-to-pdf={HERE / (STEM + '.pdf')}", html.as_uri(),
        ], check=True, timeout=60, capture_output=True)
    with fitz.open(HERE / f"{STEM}.pdf") as pdf:
        assert len(pdf) == 1
        page = pdf[0]
        assert page.rect.width > page.rect.height, "Preserve landscape layout"
        lines = [line for block in page.get_text("dict")["blocks"] if "lines" in block
                 for line in block["lines"]]
        assert any("Fig. 1. XV LVAS ventilation map." in "".join(s["text"] for s in line["spans"])
                   for line in lines)
        assert "UI for interfacing." in page.get_text()
        assert any("Support repeatable breathing cycles." in "".join(s["text"] for s in line["spans"])
                   for line in lines), "Objective must remain complete on one line"
        page.get_pixmap(matrix=fitz.Matrix(.625, .625), alpha=False).save(
            HERE / f"{STEM}_preview.png")
        page.get_pixmap(clip=fitz.Rect(0, 21.6 / 2.54 * 72, 38.4 / 2.54 * 72,
                                     33.9 / 2.54 * 72), alpha=False).save(
            HERE / f"{STEM}_fig1_preview.png")
    print("Updated specified landscape HTML/PDF/preview; unrelated elements unchanged.")


if __name__ == "__main__":
    main()
