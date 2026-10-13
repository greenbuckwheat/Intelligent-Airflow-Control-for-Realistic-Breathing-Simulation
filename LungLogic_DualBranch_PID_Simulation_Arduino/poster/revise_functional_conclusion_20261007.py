"""Update final poster conclusion and Fig. 8 without disturbing other panels."""
import subprocess
import tempfile

from bs4 import BeautifulSoup
import fitz
from update_physical_figures_20261007 import HERE, position

STEM = "LungLogic_Poster_2026-10-07_4DMedical"
CONCLUSION = (
    "Physical tests refined model parameters and showed trends consistent with the model: "
    "higher PPV voltage restricts airflow and slows filling and emptying near closure. "
    "Measured responses guide phase-specific valve settings for adjustable flow and repeatable cycles. "
    "Future work will reuse this system with the medical team's biomimetic lung."
)


def main():
    html = HERE / f"{STEM}.html"
    soup = BeautifulSoup(html.read_text(encoding="utf-8"), "html.parser")
    boxes = soup.select("article.poster div.text")
    conclusion = next(e for e in boxes if e.get_text().startswith(
        ("Open-loop tests identified", "Physical experiments refined")))
    conclusion.string = CONCLUSION
    # Expand into existing blank space below Fig. 7, retaining 36 pt body style.
    position(conclusion, top=107.3, height=9.25)
    caption = next(e for e in boxes if e.get_text().startswith("Fig. 8."))
    caption.string = "Fig. 8. Inspiratory and expiratory pinch-valve opening tests and time constants."
    html.write_text(str(soup), encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="lunglogic_conclusion_") as profile:
        subprocess.run([
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            "--headless", "--disable-gpu", "--no-pdf-header-footer", "--no-first-run",
            "--disable-extensions", f"--user-data-dir={profile}",
            f"--print-to-pdf={HERE / (STEM + '.pdf')}", html.as_uri(),
        ], check=True, timeout=60, capture_output=True)
    with fitz.open(HERE / f"{STEM}.pdf") as pdf:
        assert len(pdf) == 1
        page = pdf[0]
        text = " ".join(page.get_text().split())
        assert "medical team's biomimetic lung." in text, "Conclusion clipped"
        assert "opening tests and time constants." in text, "Caption clipped"
        page.get_pixmap(alpha=False).save(HERE / f"{STEM}_preview.png")
        page.get_pixmap(clip=fitz.Rect(0, 87.6 / 2.54 * 72, page.rect.width,
                                     117.3 / 2.54 * 72), alpha=False).save(
            HERE / f"{STEM}_results_preview.png")
    print("Updated PDF/HTML/preview; verified complete conclusion and Fig. 8 on one A0 page.")


if __name__ == "__main__":
    main()
