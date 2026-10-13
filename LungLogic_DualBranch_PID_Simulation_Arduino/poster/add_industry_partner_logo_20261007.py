"""Place supplied 4DMedical artwork without altering its pixels or proportions."""
import base64
from pathlib import Path
import subprocess
import tempfile

from bs4 import BeautifulSoup
import fitz
from update_physical_figures_20261007 import HERE, position

STEM = "LungLogic_Poster_2026-10-07_4DMedical"


def main():
    source = HERE / "LungLogic_Poster_2026-10-07_ATM_VAC_revised.html"
    original = BeautifulSoup(source.read_text(encoding="utf-8"), "html.parser")
    soup = BeautifulSoup(str(original), "html.parser")
    article = soup.select_one("article.poster")
    footer = next(e for e in article.select("div.text")
                  if e.get_text().startswith("Industry collaborator:"))
    footer.string = "Industry collaborator: 4DMedical |Reference: XV LVAS Info Sheet, DOC-5070 US V1 (2023) |"
    position(footer, width=72.0)

    def picture(path, alt, left, top, width, height, background=None):
        style = (f"left:{left:.4f}cm;top:{top:.4f}cm;width:{width:.4f}cm;"
                 f"height:{height:.4f}cm;object-fit:contain;")
        if background:
            style += f"background:{background};"
        article.append(soup.new_tag("img", attrs={
            "class": "item picture", "alt": alt, "style": style,
            "src": "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii"),
        }))

    picture(Path(r"D:\Downloads\4DMedical_Social_Icon.png"),
            "4DMedical industry partner logo", 72.3, .1, 11.0, 11.0)
    label = soup.new_tag("div", attrs={
        "class": "item text",
        "style": "left:72.3000cm;top:10.3000cm;width:11.0000cm;height:1.4500cm;"
                 "font-size:28.0pt;font-weight:400;font-style:italic;color:#FFFFFF;"
                 "text-align:center;background:#000643;",
    })
    label.string = "Industry Partner"
    article.append(label)
    # A navy footer badge makes the supplied reversed white wordmark legible.
    badge = soup.new_tag("div", attrs={
        "class": "item shape",
        "style": "left:75.0000cm;top:117.3000cm;width:8.4000cm;height:1.3500cm;"
                 "background:#000643;",
    })
    article.append(badge)
    picture(Path(r"D:\Downloads\4DMedical_Logo_CMYK_Rev.png"),
            "4DMedical reversed white footer wordmark", 75.6, 117.33, 7.2, 1.29)

    # Verify all prior content/typography except the explicitly requested footer.
    existing = article.select(":scope > *")
    for element in original.select("article.poster > *"):
        if element.get_text().startswith("Industry collaborator:"):
            continue
        assert any(str(element) == str(candidate) for candidate in existing)
    html = HERE / f"{STEM}.html"
    html.write_text(str(soup), encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="lunglogic_brand_") as profile:
        subprocess.run([
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            "--headless", "--disable-gpu", "--no-pdf-header-footer", "--no-first-run",
            "--disable-extensions", f"--user-data-dir={profile}",
            f"--print-to-pdf={HERE / (STEM + '.pdf')}", html.as_uri(),
        ], check=True, timeout=60, capture_output=True)
    with fitz.open(HERE / f"{STEM}.pdf") as pdf:
        assert len(pdf) == 1
        page = pdf[0]
        assert "Industry Partner" in page.get_text()
        page.get_pixmap(alpha=False).save(HERE / f"{STEM}_preview.png")
        page.get_pixmap(clip=fitz.Rect(0, 0, page.rect.width, 12.3 / 2.54 * 72),
                        alpha=False).save(HERE / f"{STEM}_header_preview.png")
        page.get_pixmap(clip=fitz.Rect(0, 117.2 / 2.54 * 72, page.rect.width, page.rect.height),
                        alpha=False).save(HERE / f"{STEM}_footer_preview.png")
    print("Exported one A0 page. Existing content and type sizes preserved; logos embedded in HTML.")
    print(HERE / f"{STEM}.pdf")


if __name__ == "__main__":
    main()
