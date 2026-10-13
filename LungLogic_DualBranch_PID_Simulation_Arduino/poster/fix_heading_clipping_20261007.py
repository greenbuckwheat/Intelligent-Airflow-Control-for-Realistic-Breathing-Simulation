"""Ensure fixed-size headings fit their boxes without shrinking their fonts."""
from collections import Counter
import re
import subprocess
import tempfile

from bs4 import BeautifulSoup
import fitz
from update_physical_figures_20261007 import HERE, position

STEM = "LungLogic_Poster_2026-10-07_4DMedical"


def main():
    html = HERE / f"{STEM}.html"
    original = html.read_text(encoding="utf-8")
    soup = BeautifulSoup(original, "html.parser")
    count = 0
    for element in soup.select("div.text"):
        style = element.get("style", "")
        if "font-weight:700" not in style:
            continue
        match = re.search(r"font-size:([0-9.]+)pt", style)
        if not match:
            continue
        size = float(match[1])
        if size not in (36, 48, 84):
            continue
        # Old 48 pt heading line plus padding exceeded its 1.778 cm box.
        # A single-line height and modest padding preserve every glyph.
        element["style"] += "line-height:1.0;padding:.06cm .18cm;"
        if size == 48:
            top = float(re.search(r"top:([0-9.]+)cm", style)[1])
            position(element, top=top - .12, height=2.05)
        elif size == 36:
            position(element, height=1.75)
        count += 1
    updated = str(soup)
    for attribute in ("font-size", "font-weight", "font-style", "color"):
        pattern = rf"(?<![\w-]){attribute}:([^;\"]+)"
        assert Counter(re.findall(pattern, original)) == Counter(re.findall(pattern, updated))
    html.write_text(updated, encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="lunglogic_headings_") as profile:
        subprocess.run([
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            "--headless", "--disable-gpu", "--no-pdf-header-footer", "--no-first-run",
            "--disable-extensions", f"--user-data-dir={profile}",
            f"--print-to-pdf={HERE / (STEM + '.pdf')}", html.as_uri(),
        ], check=True, timeout=60, capture_output=True)
    with fitz.open(HERE / f"{STEM}.pdf") as pdf:
        assert len(pdf) == 1
        page = pdf[0]
        assert "3. Integrated Control System & Software" in page.get_text()
        page.get_pixmap(alpha=False).save(HERE / f"{STEM}_preview.png")
        page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), clip=fitz.Rect(
            0, 58.3 / 2.54 * 72, page.rect.width, 62.5 / 2.54 * 72),
            alpha=False).save(HERE / f"{STEM}_heading_preview.png")
    print(f"Adjusted {count} heading boxes; retained font sizes and styles; exported one-page A0 PDF.")


if __name__ == "__main__":
    main()
