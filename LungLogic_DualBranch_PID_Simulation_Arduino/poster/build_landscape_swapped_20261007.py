"""Create a separate landscape variant with sections 3 and 4 exchanged."""
from pathlib import Path
import re, subprocess, tempfile
from bs4 import BeautifulSoup
import fitz

HERE=Path(__file__).resolve().parent
STEM='LungLogic_Swapped_Poster_2026-10-07'

def main():
    soup=BeautifulSoup((HERE/'LungLogic_Poster_2026-10-07.html').read_text(encoding='utf-8'),'html.parser')
    items=soup.select('.poster > *')
    def place(i,x,y,w,h):
        for key,value in zip(('left','top','width','height'),(x,y,w,h)):
            items[i]['style']=re.sub(rf'(?<![\w-]){key}:[^;]+;',f'{key}:{value:.4f}cm;',items[i]['style'])
    # Section 3: wide lower-left panel, with UI and feedback side by side.
    place(39,.7,54,76.9,27.8); place(40,.7,54,76.9,2.3876)
    place(41,1.15,54.18,76,2.05)
    items[41].clear(); items[41].append('3. Integrated Control System & Software')
    place(42,1.4,56.8,75.5,1.75); place(43,1.4,58.5,75.5,3)
    place(44,1.4,61.8,38,1.75)
    place(45,1.4,63.4,18.6,1.75); place(46,21.1,63.4,18.6,1.75)
    place(47,1.4,65.2,18.6,11.829); place(48,21.1,65.2,18.6,11.869)
    place(49,1.4,77.8,38.3,1.8288)
    place(50,40.5,61.6,36.4,1.75)
    place(51,46.2,63.65,25.1,8.693)
    place(52,40.5,72.8,36.4,2.6)
    place(53,40.5,75.2,36.4,1.75); place(54,40.5,77.2,36.4,4.4)
    # Section 4: right column, with simulation above physical measurements.
    place(55,78.3,13,39.9,68.8); place(56,78.3,13,39.9,2.3876)
    place(57,78.75,13.18,39,2.05)
    place(58,79,16.1,38.5,1.75)
    place(60,81.75,18.3,33,10.469)
    place(61,79,29.5,38.5,2.5)
    place(63,79,32.7,38.5,9.4)
    place(59,79,43.3,38.5,1.75)
    for i,x,y in [(67,79.2,46),(68,98.9,46),(69,79.2,55.5),(70,98.9,55.5)]:
        place(i,x,y,18.6,8.9)
    place(62,79,65.1,38.5,3)
    soup.title.string='LungLogic A0 Landscape Poster — Sections 3 and 4 Swapped'
    html=HERE/f'{STEM}.html'
    html.write_text(str(soup),encoding='utf-8')
    with tempfile.TemporaryDirectory(prefix='lunglogic_swapped_') as profile:
        subprocess.run([r'C:\Program Files\Google\Chrome\Application\chrome.exe','--headless','--disable-gpu','--no-pdf-header-footer','--no-first-run','--disable-extensions',f'--user-data-dir={profile}',f'--print-to-pdf={HERE/(STEM+".pdf")}',html.as_uri()],check=True,timeout=90,capture_output=True)
    with fitz.open(HERE/f'{STEM}.pdf') as doc:
        assert len(doc)==1
        doc[0].set_mediabox(fitz.Rect(0,0,1189/25.4*72,841/25.4*72))
        doc.saveIncr()
        doc[0].get_pixmap(dpi=45,alpha=False).save(HERE/f'{STEM}_preview.png')
        doc[0].get_pixmap(dpi=300,alpha=False).save(HERE/f'{STEM}_300dpi.png')
    print('Saved separate A0 landscape PDF and previews:',STEM)

if __name__=='__main__': main()
