"""Repair clipped poster text and reorganise Introduction, preserving sizes."""
import base64
from collections import Counter
from pathlib import Path
import re
import subprocess
import tempfile

from bs4 import BeautifulSoup
import fitz
from update_physical_figures_20261007 import HERE, position

STEM = "LungLogic_Poster_2026-10-07_ATM_VAC_revised"


def main():
    source = HERE / "LungLogic_Poster_2026-10-07_ATM_VAC.html"
    document = source.read_text(encoding="utf-8")
    soup = BeautifulSoup(document, "html.parser")
    article = soup.select_one("article.poster")
    sizes = Counter(re.findall(r"font-size:([0-9.]+pt)", document))

    def find(prefix):
        matches = [e for e in article.select("div.text") if e.get_text().startswith(prefix)]
        if len(matches) != 1:
            raise ValueError(f"Expected one text box: {prefix}")
        return matches[0]

    heading = find("1.1 Project need")
    heading.string = "1.1 Project background"
    background = find("Adjustable RR, I:E, tidal volume")
    objectives = find("Commercial models cannot meet")
    background.string = (
        "Commercial models cannot meet specific research and development needs. "
        "4DMedical's XV scanner enables research using our controller and the medical team's lung model. "
        "This stage validates one 250 mL branch prototype."
    )
    position(background, top=17.399, height=6.5)
    objectives_heading = find("1.2 Project objective")
    position(objectives_heading, left=16.8, top=24.4, width=23.7)
    objectives.clear()
    points = soup.new_tag("ul", attrs={"style": "margin:0;padding-left:1.0cm;"})
    for text in (
        "Adjust breathing rate and I:E.",
        "Control tidal volume.",
        "Program regional airflow.",
        "Support repeatable breathing cycles.",
        "UI for interfacing.",
    ):
        point = soup.new_tag("li", attrs={"style": "margin:0 0 .18cm 0;"})
        point.string = text
        points.append(point)
    objectives.append(points)
    position(objectives, left=16.8, top=26.2, width=23.7, height=9.2)

    image = article.find("img", alt="4DMedical XV LVAS regional ventilation visualisation")
    position(image, left=2.6, top=24.4, width=11.8, height=11.8 * 12.0061 / 14.64)
    caption = find("Fig. 1.")
    caption.string = "Fig. 1. XV LVAS ventilation map."
    position(caption, left=1.397, top=34.2, width=14.8, height=1.6)

    position(find("1.3 Nominal breathing reference"), top=36.1)
    breathing = article.find("img", alt="Nominal single-branch breathing cycle")
    position(breathing, left=8.7, top=37.8, width=24.52, height=8.1)
    caption = find("Fig. 2.")
    caption.string = "Fig. 2. Nominal breathing cycle: 15 bpm; I:E = 1:2."
    position(caption, top=46.05, height=1.2)
    # The full five/six-line paragraph must fit above the existing Fig. 4.
    position(find("MCU executes commands from UI"), height=9.1)
    # Fix corrupted characters already present in the supplied HTML.
    for element in article.select("div.text"):
        if "font-size:25.0pt" in element.get("style", "") and "text-align:center" in element.get("style", ""):
            element.string = "→"
    for cell in article.select("table td, table th"):
        if "nominal 15 bpm" in cell.get_text():
            cell.string = "12–16 bpm; nominal 15 bpm"

    updated = str(soup)
    assert Counter(re.findall(r"font-size:([0-9.]+pt)", updated)) == sizes
    assert not re.search(r"(Fig\. [12]\. ){2}", updated)
    # Physical measurements and the complete control/software section unchanged.
    original = BeautifulSoup(document, "html.parser")
    for element in original.select("article.poster > *"):
        m = re.search(r"top:([0-9.]+)cm", element.get("style", ""))
        if m and float(m[1]) >= 58:
            assert any(str(element) == str(e) for e in article.select(":scope > *"))
    html = HERE / f"{STEM}.html"
    html.write_text(updated, encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="lunglogic_intro_") as profile:
        subprocess.run([
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            "--headless", "--disable-gpu", "--no-pdf-header-footer", "--no-first-run",
            "--disable-extensions", f"--user-data-dir={profile}",
            f"--print-to-pdf={HERE / (STEM + '.pdf')}", html.as_uri(),
        ], check=True, timeout=60, capture_output=True)
    with fitz.open(HERE / f"{STEM}.pdf") as pdf:
        assert len(pdf) == 1
        page = pdf[0]
        text = page.get_text()
        assert "sensor interfaces." in text
        assert "Support repeatable breathing cycles." in text
        assert "UI for interfacing." in text
        page.get_pixmap(alpha=False).save(HERE / f"{STEM}_preview.png")
        page.get_pixmap(clip=fitz.Rect(0, 13 / 2.54 * 72, page.rect.width, 57.3 / 2.54 * 72),
                        alpha=False).save(HERE / f"{STEM}_intro_preview.png")
    print("Validated: single-page A0, unchanged font sizes, captions repaired, all paragraph endings present.")
    print(HERE / f"{STEM}.pdf")


if __name__ == "__main__":
    main()
