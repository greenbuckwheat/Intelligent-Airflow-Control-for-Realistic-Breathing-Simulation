"""Add supplied QR beneath PCB photo; keep typography and other panels intact."""
import base64
from pathlib import Path
import shutil
import subprocess
import tempfile

from bs4 import BeautifulSoup
import fitz
from update_physical_figures_20261007 import HERE, position

STEM = "LungLogic_Poster_2026-10-07_4DMedical"


def main():
    html = HERE / f"{STEM}.html"
    backup = HERE / f"{STEM}_before_pcb_qr.html"
    if not backup.exists():
        shutil.copy2(html, backup)
    soup = BeautifulSoup(html.read_text(encoding="utf-8"), "html.parser")
    article = soup.select_one("article.poster")
    photo = article.find("img", alt="Bridge PCB connected to STM32")
    # Text, large PCB photo, then a smaller QR in adjacent columns.
    position(photo, left=67.8, top=44.6436, width=9.0, height=9.3333)
    paragraph = next(e for e in article.select("div.text")
                     if e.get_text().startswith("MCU executes commands from UI"))
    position(paragraph, width=23.2, height=9.1)
    caption = next(e for e in article.select("div.text") if e.get_text().startswith("Fig. 4."))
    position(caption, width=39.2684)
    for element in article.select('[data-pcb-qr="true"]'):
        element.decompose()
    qr = soup.new_tag("img", attrs={
        "class": "item picture", "data-pcb-qr": "true",
        "alt": "Scan to explore PCB functions",
        "src": "data:image/png;base64," + base64.b64encode(
            Path(r"C:\Users\13922\Desktop\pcb介绍.png").read_bytes()).decode("ascii"),
        "style": "left:77.4000cm;top:46.2000cm;width:4.6000cm;height:4.6000cm;"
                 "object-fit:contain;padding:.35cm;background:#FFFFFF;",
    })
    article.append(qr)
    label = soup.new_tag("div", attrs={
        "class": "item text", "data-pcb-qr": "true",
        "style": "left:76.9000cm;top:51.3000cm;width:5.6000cm;height:1.8000cm;"
                 "font-size:18.0pt;font-weight:400;font-style:normal;"
                 "color:#17324D;text-align:center;line-height:1.05;",
    })
    label.string = "Scan for more details"
    article.append(label)
    html.write_text(str(soup), encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="lunglogic_pcbqr_") as profile:
        subprocess.run([
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            "--headless", "--disable-gpu", "--no-pdf-header-footer", "--no-first-run",
            "--disable-extensions", f"--user-data-dir={profile}",
            f"--print-to-pdf={HERE / (STEM + '.pdf')}", html.as_uri(),
        ], check=True, timeout=60, capture_output=True)
    with fitz.open(HERE / f"{STEM}.pdf") as pdf:
        assert len(pdf) == 1
        page = pdf[0]
        assert "Scan for more details" in " ".join(page.get_text().split())
        page.get_pixmap(alpha=False).save(HERE / f"{STEM}_preview.png")
        page.get_pixmap(clip=fitz.Rect(42 / 2.54 * 72, 39 / 2.54 * 72,
                                     83.4 / 2.54 * 72, 57.15 / 2.54 * 72),
                        alpha=False).save(HERE / f"{STEM}_pcb_qr_preview.png")
    print("QR embedded unchanged with extra white margin; exported one-page A0 PDF and previews.")


if __name__ == "__main__":
    main()
