"""Enlarge Fig. 1 and keep its caption on one line without changing type sizes."""
import subprocess
import tempfile

from bs4 import BeautifulSoup
import fitz
from update_physical_figures_20261007 import HERE, position

STEM = "LungLogic_Poster_2026-10-07_4DMedical"


def main():
    html = HERE / f"{STEM}.html"
    soup = BeautifulSoup(html.read_text(encoding="utf-8"), "html.parser")
    image = soup.find("img", alt="4DMedical XV LVAS regional ventilation visualisation")
    position(image, left=2.2, top=23.4, width=13.4, height=13.4 * 12.0061 / 14.64)
    boxes = soup.select("div.text")
    caption = next(e for e in boxes if e.get_text().startswith("Fig. 1."))
    position(caption, top=34.6, width=17.4, height=1.3)
    caption["style"] += "white-space:nowrap;"
    heading = next(e for e in boxes if e.get_text().startswith("1.2 Project objective"))
    objectives = next(e for e in boxes if e.find("ul"))
    position(heading, left=19.2, width=21.3)
    position(objectives, left=19.2, width=21.3, height=9.6)
    objectives["style"] += "line-height:1.05;"
    html.write_text(str(soup), encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="lunglogic_fig1_") as profile:
        subprocess.run([
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            "--headless", "--disable-gpu", "--no-pdf-header-footer", "--no-first-run",
            "--disable-extensions", f"--user-data-dir={profile}",
            f"--print-to-pdf={HERE / (STEM + '.pdf')}", html.as_uri(),
        ], check=True, timeout=60, capture_output=True)
    with fitz.open(HERE / f"{STEM}.pdf") as pdf:
        assert len(pdf) == 1
        page = pdf[0]
        lines = [line for block in page.get_text("dict")["blocks"] if "lines" in block
                 for line in block["lines"]]
        assert any("Fig. 1. XV LVAS ventilation map." in "".join(s["text"] for s in line["spans"])
                   for line in lines), "Fig. 1 caption must remain complete on one line"
        assert "UI for interfacing." in page.get_text()
        page.get_pixmap(alpha=False).save(HERE / f"{STEM}_preview.png")
        page.get_pixmap(clip=fitz.Rect(0, 23 / 2.54 * 72, 41.6 / 2.54 * 72,
                                     36.1 / 2.54 * 72), alpha=False).save(
            HERE / f"{STEM}_fig1_preview.png")
    print("Validated single-line Fig. 1 caption; enlarged image; complete objectives; updated PDF/HTML.")


if __name__ == "__main__":
    main()
