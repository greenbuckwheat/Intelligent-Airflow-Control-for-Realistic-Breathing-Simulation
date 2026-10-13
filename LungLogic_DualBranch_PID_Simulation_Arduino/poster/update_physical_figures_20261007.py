"""Replace only Physical measurements figures, keeping poster typography.

Create a separate dated HTML/PDF/preview; preserve the supplied originals.
"""
from __future__ import annotations

import base64
from collections import Counter
from pathlib import Path
import re
import subprocess
import tempfile

from bs4 import BeautifulSoup
import fitz

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "LungLogic_Poster_2026-10-02_physical_lung_future_work.html"
STEM = "LungLogic_Poster_2026-10-07_ATM_VAC"
FIGURES = HERE.parents[1] / "实验" / "flow_control_exp" / "analysis" / "ppt_assets_today_atm_vac"


def position(element, **values):
    style = element["style"]
    for key, value in values.items():
        style = re.sub(rf"{key}:[0-9.]+cm;", f"{key}:{value:.4f}cm;", style)
    element["style"] = style


def main():
    document = SOURCE.read_text(encoding="utf-8")
    soup = BeautifulSoup(document, "html.parser")
    original_sizes = Counter(re.findall(r"font-size:([0-9.]+pt)", document))
    article = soup.select_one("article.poster")
    obsolete = [img for img in article.select("img") if img.get("alt") in (
        "Measured inspiration flow at different PPV voltages",
        "Identified first-order physical-branch dynamics",
    )]
    if len(obsolete) != 2:
        raise ValueError("Expected exactly two original physical figures")
    for img in obsolete:
        img.decompose()
    # Two columns within the original physical-results column: flow / tau.
    # Rows distinguish ATM inspiration and VAC expiration. No font resizing.
    for row, valve in enumerate(("atm", "vac")):
        for column, name in enumerate(("representative_flow_curves", "tau_voltage_fit")):
            path = FIGURES / f"{name}_{valve}.png"
            img = soup.new_tag("img", attrs={
                "class": "item picture",
                "alt": f"2026-10-07 {valve.upper()} {'flow response' if column == 0 else 'effective time constant'}",
                "src": "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii"),
                "style": f"left:{36.8 + column * 23.1:.4f}cm;top:{92.25 + row * 11.05:.4f}cm;"
                         "width:22.6000cm;height:10.8000cm;object-fit:contain;",
            })
            article.append(img)
    captions = [e for e in article.select("div.text") if e.get_text().startswith("Fig. 8.")]
    if len(captions) != 1:
        raise ValueError("Expected original Fig. 8 caption")
    captions[0].string = "Fig. 8. ATM / VAC flow responses and effective time constants (7 Oct)."
    position(captions[0], top=114.45, height=2.0)
    summaries = [e for e in article.select("div.text")
                 if e.get_text().startswith("The measured PPV response qualitatively")]
    if len(summaries) != 1:
        raise ValueError("Expected original results summary")
    summaries[0].string = (
        "Open-loop tests identified distinct inspiration and expiration dynamics: "
        "plateau time constants of 2.53 s (ATM) and 1.31 s (VAC). "
        "Near closure, both responses slow. These are effective fits, not isolated valve delays."
    )
    position(summaries[0], width=35.0, height=8.8)
    updated = str(soup)
    assert Counter(re.findall(r"font-size:([0-9.]+pt)", updated)) == original_sizes
    # All existing nodes outside the two images, Fig. 8, and summary are unchanged.
    original = BeautifulSoup(document, "html.parser")
    before = original.select("article.poster > *")
    after = article.select(":scope > *")
    changed_texts = ("Fig. 8.", "The measured PPV response qualitatively")
    for element in before:
        if element.name == "img" and element.get("alt") in (
            "Measured inspiration flow at different PPV voltages",
            "Identified first-order physical-branch dynamics",
        ):
            continue
        if element.get_text().startswith(changed_texts):
            continue
        assert any(str(element) == str(candidate) for candidate in after), "Unrelated poster element changed"
    html_path = HERE / f"{STEM}.html"
    html_path.write_text(updated, encoding="utf-8")
    browser = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    with tempfile.TemporaryDirectory(prefix="lunglogic_poster_") as profile:
        subprocess.run([
            str(browser), "--headless", "--disable-gpu", "--no-pdf-header-footer",
            "--no-first-run", "--disable-extensions", f"--user-data-dir={profile}",
            f"--print-to-pdf={HERE / (STEM + '.pdf')}", html_path.as_uri(),
        ], check=True, timeout=60, capture_output=True)
    with fitz.open(HERE / f"{STEM}.pdf") as pdf:
        assert len(pdf) == 1, "Poster must remain one A0 page"
        page = pdf[0]
        expected = (841 / 25.4 * 72, 1189 / 25.4 * 72)
        assert abs(page.rect.width - expected[0]) < 2
        assert abs(page.rect.height - expected[1]) < 2
        page.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False).save(HERE / f"{STEM}_preview.png")
        page.get_pixmap(matrix=fitz.Matrix(1, 1), clip=fitz.Rect(
            0, 87.6 / 2.54 * 72, page.rect.width, 117.3 / 2.54 * 72
        ), alpha=False).save(HERE / f"{STEM}_results_preview.png")
        assert "Near closure, both responses slow" in page.get_text()
    print("Validated: one A0 page, four replacement figures, unchanged font sizes and unrelated elements.")
    print(HERE / f"{STEM}.pdf")


if __name__ == "__main__":
    main()
