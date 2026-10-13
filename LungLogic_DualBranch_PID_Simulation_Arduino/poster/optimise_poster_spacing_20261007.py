"""Add breathing-room around Fig. 2 without changing poster body typography."""
from collections import Counter
import re
import shutil
import subprocess
import tempfile

from bs4 import BeautifulSoup
import fitz
from update_physical_figures_20261007 import HERE, position

STEM = "LungLogic_Poster_2026-10-07_4DMedical"


def main():
    html = HERE / f"{STEM}.html"
    document = html.read_text(encoding="utf-8")
    backup = HERE / f"{STEM}_before_spacing.html"
    if not backup.exists():
        shutil.copy2(html, backup)
    soup = BeautifulSoup(document, "html.parser")

    def find(prefix):
        return next(e for e in soup.select("div.text") if e.get_text().startswith(prefix))

    # Separate heading -> chart -> caption -> table -> note, in the same panel.
    plot = soup.find("img", alt="Nominal single-branch breathing cycle")
    position(plot, left=9.15, top=38.05, width=23.61, height=7.8)
    caption = find("Fig. 2.")
    position(caption, top=46.2, height=1.3)
    caption["style"] += "line-height:1.08;"
    table = soup.select_one("table.data-table")
    position(table, top=48.15, height=5.25)
    table["style"] += "line-height:1.04;"
    position(find("Nominal reference values shown"), top=54.0, height=.9)
    position(find("Source: 4DMedical"), top=55.15, height=1.0)
    # Compact multiline paragraphs slightly; preserve font, size, weight, colour.
    for prefix in ("Commercial models cannot meet", "MCU executes commands from UI",
                   "Track flow and tidal volume", "Auto PID scores bounded",
                   "Open-loop tests identified"):
        element = find(prefix)
        element["style"] += "line-height:1.05;"
    updated = str(soup)
    for attribute in ("font-size", "font-weight", "font-style", "color"):
        pattern = rf"(?<![\w-]){attribute}:([^;\"]+)"
        assert Counter(re.findall(pattern, document)) == Counter(re.findall(pattern, updated))
    html.write_text(updated, encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="lunglogic_spacing_") as profile:
        subprocess.run([
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            "--headless", "--disable-gpu", "--no-pdf-header-footer", "--no-first-run",
            "--disable-extensions", f"--user-data-dir={profile}",
            f"--print-to-pdf={HERE / (STEM + '.pdf')}", html.as_uri(),
        ], check=True, timeout=60, capture_output=True)
    with fitz.open(HERE / f"{STEM}.pdf") as pdf:
        assert len(pdf) == 1
        page = pdf[0]
        page.get_pixmap(alpha=False).save(HERE / f"{STEM}_preview.png")
        page.get_pixmap(clip=fitz.Rect(0, 35.7 / 2.54 * 72, 41.6 / 2.54 * 72,
                                     56.8 / 2.54 * 72), alpha=False).save(
            HERE / f"{STEM}_spacing_preview.png")
        caption_blocks = [b for b in page.get_text("blocks") if "Fig. 2." in b[4]]
        table_blocks = [b for b in page.get_text("blocks") if "Respiratory rate" in b[4]]
        assert len(caption_blocks) == len(table_blocks) == 1
        gap = table_blocks[0][1] - caption_blocks[0][3]
        assert gap > 5, f"Caption/table gap too small: {gap} pt"
        print(f"Validated: one A0 page; unchanged body typography; Fig. 2/table text gap {gap:.1f} pt.")


if __name__ == "__main__":
    main()
