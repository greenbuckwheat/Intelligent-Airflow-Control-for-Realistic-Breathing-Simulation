"""Reflow the approved poster to A0 landscape, preserving existing font sizes."""
from pathlib import Path
import re, base64, subprocess, tempfile, json
from collections import Counter
from bs4 import BeautifulSoup
from PIL import Image
from io import BytesIO
import fitz

HERE = Path(__file__).resolve().parent
STEM = 'LungLogic_Revised_Title_Poster_2026-10-07'

def main():
    original = (HERE / 'LungLogic_Poster_2026-10-07_4DMedical.html').read_text(encoding='utf-8')
    soup = BeautifulSoup(original, 'html.parser')
    items = soup.select('.poster > *')
    def place(i, x, y, w, h):
        e = items[i]
        for k, v in zip(('left','top','width','height'), (x,y,w,h)):
            e['style'] = re.sub(rf'(?<![\w-]){k}:[^;]+;', f'{k}:{v:.4f}cm;', e['style'])
        if e.name == 'img':
            e['style'] += 'object-fit:contain;'
    # Full-width title and metadata; keep 84 / 36 pt typography.
    place(0,0,0,118.9,9.8)
    place(1,.7,.75,107,3.5)
    items[1].string = 'LungLogic: Intelligent Airflow Control'
    place(2,.7,4.05,107,3.5)
    place(3,.7,7.8,110.5,1.778)
    items[3].append(' | Discipline: Mechatronics Engineering')
    items[3]['style'] += 'white-space:nowrap;'
    place(71,109.2,.1,9.1,9.1)
    place(72,109.2,8.5,9.1,1.1)
    items[72]['style'] += 'padding:0;white-space:nowrap;'
    # Left column: introduction.
    place(4,.7,13,37.7,40.3); place(5,.7,13,37.7,2.3876)
    place(6,1.15,13.18,36.8,2.05)
    place(7,1.4,15.95,36.3,1.75); place(8,1.4,17.65,36.3,6.5)
    place(65,1.8,24.8,10.8,8.857); place(66,1.4,33.8,13.5,2.3)
    place(9,15,25.1,22.7,1.75); place(10,15,27,22.7,9.3)
    place(11,1.4,36.3,36.3,1.75)
    place(12,8.5,37.9,22,7.267); place(13,1.4,45.35,36.3,1.3)
    place(14,1.4,46.85,36.3,4.1)
    items[14]['style'] += 'height:auto;'
    for td in items[14].select('td'): td['style'] = 'padding:.04cm .10cm;'
    for row in items[14].select('tr'):
        cells=row.select('td')
        cells[0]['style'] += 'width:40%;'
        cells[1]['style'] += 'width:60%;'
    place(15,1.4,51.5,36.3,.75); place(16,1.4,52.35,36.3,.75)
    for i in (15,16): items[i]['style'] += 'padding:0 .18cm;'
    # Middle column: prototype and embedded architecture.
    place(17,39.1,13,38.5,40.3); place(18,39.1,13,38.5,2.3876)
    place(19,39.55,13.18,37.6,2.05)
    place(20,39.8,15.95,37.1,1.75)
    place(21,42.6,18,10.4,16); place(22,59.2,18,10.4,16.081)
    place(23,39.8,34.25,37.1,2.4)
    place(24,39.8,36.85,37.1,1.75)
    # The four original architecture boxes and arrows retain their font sizes.
    for a,b,x,w in [(25,26,39.9,7.7),(27,28,48.5,7.7),(29,30,57.1,8.8),(31,32,66.8,9.6)]:
        place(a,x,38.9,w,2.9464); place(b,x+.15,39.53,w-.3,1.6764)
    for i,x in [(33,47.65),(34,56.25),(35,65.95)]:
        place(i,x,39.58,.95,1.3); items[i].string = '\u2192'
        items[i]['style'] += 'padding:0;border:0;line-height:1;'
    place(36,39.8,42.4,23.1,8.3)
    place(37,63.4,42.6,8.4,8.711)
    place(75,72.2,44.4,4.6,4.6); place(76,71.6,49.2,5.8,1.8)
    place(38,39.8,51.2,37.1,1.95)
    # Right column: software, with a clear vertical reading order.
    place(39,78.3,13,39.9,68.8); place(40,78.3,13,39.9,4.4)
    place(41,78.75,13.18,39,4.05)
    items[41].clear()
    items[41].append('3. Integrated Control System & Software')
    place(42,79,18,38.5,1.75); place(43,79,19.85,38.5,6)
    place(44,79,26.65,38.5,3.1)
    place(45,79.2,30.3,18.6,1.75); place(46,98.9,30.3,18.6,1.75)
    # Fill the column with larger screenshots at their native aspect ratios.
    # Report their source-limited DPI honestly rather than upsampling the pixels.
    place(47,79.2,32.5,18.6,11.829); place(48,98.9,32.5,18.6,11.869)
    place(49,79,45.2,38.5,1.8288)
    place(50,79,48.3,38.5,1.75)
    place(51,84.25,51,25.1,8.693)
    place(52,79,61,38.5,2.6)
    place(53,79,65,38.5,3.6); place(54,79,69.3,38.5,6)
    # Results span the first two columns.
    place(55,.7,54,76.9,27.8); place(56,.7,54,76.9,2.3876)
    place(57,1.15,54.18,76,2.05)
    place(58,1.4,56.7,33,1.75); place(59,35.5,56.7,41.4,1.75)
    place(60,1.4,59,28.2,8.947); place(61,1.4,68.3,33,2.5)
    place(63,1.4,71.1,33,10.3)
    for i,x,y in [(67,35.5,59),(68,56.45,59),(69,35.5,69.15),(70,56.45,69.15)]:
        place(i,x,y,20.4,9.8)
    place(62,35.5,79.2,41.4,2.4)
    place(64,.9,82.65,106,.84)
    place(73,109,82.5,9.2,1.35); place(74,109.5,82.53,8.2,1.29)
    # Reclaim the shortened header's 5.6 cm for the main content.
    for i in list(range(4,64)) + [65,66,67,68,69,70,75,76]:
        e=items[i]
        top=float(re.search(r'top:([\d.]+)cm',e['style'])[1])
        e['style']=re.sub(r'top:[^;]+;',f'top:{top-5.6:.4f}cm;',e['style'])
    place(66,1.4,28.2,13.5,2.5)
    place(55,.7,48.4,76.9,33.4)
    # Spread the result plots and conclusion through the expanded lower panel.
    place(63,1.4,66,33,12.5)
    for i,x,y in [(67,35.5,53.4),(68,56.45,53.4),(69,35.5,65.4),(70,56.45,65.4)]:
        place(i,x,y,20.4,9.8)
    place(62,35.5,77,41.4,2.4)
    # Stack the two larger UI screenshots in the right column.
    place(39,78.3,7.4,39.9,74.4); place(40,78.3,7.4,39.9,4.4)
    place(41,78.75,7.58,39,4.05)
    place(42,79,12.4,38.5,1.75); place(43,79,14.2,38.5,4.5)
    place(44,79,19.4,38.5,1.75)
    place(45,85.75,21.4,25,1.75); place(47,85.75,23.3,25,15.9)
    place(46,85.75,40.1,25,1.75); place(48,85.75,41.9,25,15.953)
    place(49,79,58.4,38.5,1.8288)
    place(50,79,61,38.5,1.75); place(51,84.25,63.2,25.1,8.693)
    place(52,79,72.3,38.5,2.6)
    place(53,79,75,38.5,1.75); place(54,79,77,38.5,4.5)
    items[64]['style'] += 'padding:0 .18cm;'
    # Two-line title: lower the content and reduce gaps, preserving image sizes.
    for i in list(range(4,39)) + list(range(55,64)) + [65,66,67,68,69,70,75,76]:
        e=items[i]
        top=float(re.search(r'top:([\d.]+)cm',e['style'])[1])
        e['style']=re.sub(r'top:[^;]+;',f'top:{top+3.2:.4f}cm;',e['style'])
    place(55,.7,51.6,76.9,30.2)
    place(60,1.4,56.6,33,10.469)
    chart=HERE/'assets'/'single_lung_validation_two_panel_print.png'
    items[60]['src']='data:image/png;base64,'+base64.b64encode(chart.read_bytes()).decode()
    place(61,1.4,67.6,33,1.7)
    place(63,1.4,69.7,33,11.8)
    for i,x in [(69,35.5),(70,56.45)]: place(i,x,66.6,20.4,9.8)
    place(62,35.5,78.4,41.4,2.4)
    place(39,78.3,10.6,39.9,71.2); place(40,78.3,10.6,39.9,4.4)
    place(41,78.75,10.78,39,4.05)
    place(42,79,15.6,38.5,1.75); place(43,79,17.4,38.5,4.5)
    # Present the objective as four compact bullets, two per row.
    items[43].clear()
    objectives=soup.new_tag('ul',attrs={'style':'display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.28cm .7cm;list-style:none;margin:0;padding:0;'})
    for objective in ('Track flow rate + tidal volume.', 'Repeatable cycle.',
                      'Feedforward valve control.', 'Feedback error correction.'):
        entry=soup.new_tag('li',attrs={'style':'display:flex;align-items:flex-start;gap:.25cm;min-width:0;'})
        marker=soup.new_tag('span',attrs={'style':'flex:none;'})
        marker.string='\u2022'
        label=soup.new_tag('span')
        label.string=objective
        entry.append(marker); entry.append(label); objectives.append(entry)
    items[43].append(objectives)
    place(44,79,22.4,38.5,1.75)
    place(45,85.75,24.2,25,1.75); place(47,85.75,25.9,25,15.9)
    place(46,85.75,42.2,25,1.75); place(48,85.75,43.9,25,15.953)
    place(49,79,60.2,38.5,1.8288)
    place(50,79,62.4,38.5,1.75); place(51,84.25,64.3,25.1,8.693)
    place(52,79,73.1,38.5,1.6)
    place(53,79,75.3,38.5,1.75); place(54,79,77.1,38.5,4.5)
    # Tighten the gap between the two-row objective list and section 3.2.
    place(43,79,17.4,38.5,3.4)
    for i in range(44,55):
        e=items[i]
        top=float(re.search(r'top:([\d.]+)cm',e['style'])[1])
        e['style']=re.sub(r'top:[^;]+;',f'top:{top-1.2:.4f}cm;',e['style'])
    # Match the other one-line section bars without changing the title font size.
    place(40,78.3,10.6,39.9,2.3876)
    place(41,78.75,10.78,39,2.05)
    items[41]['style'] += 'white-space:nowrap;'
    for i in range(42,55):
        e=items[i]
        top=float(re.search(r'top:([\d.]+)cm',e['style'])[1])
        e['style']=re.sub(r'top:[^;]+;',f'top:{top-2.0124:.4f}cm;',e['style'])
    # Repair source text encoding without altering meaning.
    for td in items[14].select('td'):
        if '16 bpm' in td.get_text(): td.string = '12\u201316 bpm; nominal 15 bpm'
    # Rebuild the low-resolution route as vector geometry, matching the source.
    valve = '''<g stroke="#333" fill="white" stroke-width="1.3"><rect x="0" y="0" width="18" height="130"/><path d="M0 9L18 27H0L9 44L18 27 M0 56H36V112H0 M0 75H36 M0 94H36 M9 130l-8 4 16 4 -16 4 16 4 -16 4 8 4" fill="none"/><path d="M3 75h29m-5 -3 5 3 -5 3" fill="none"/></g>'''
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="379" height="583" viewBox="0 0 379 583"><rect width="379" height="583" fill="white"/><rect x="29" y="7" width="320" height="404" fill="none" stroke="#333" stroke-width="1.3"/><g transform="translate(87 49)">{valve}</g><g transform="translate(261 49)">{valve}</g><g stroke="#333" stroke-width="1.4" fill="white"><path d="M176 263l24 36h-24l24 -36z"/><path d="M176 263l12 16 12 -16z" fill="#111"/><ellipse cx="189" cy="355" rx="19" ry="19"/><path d="M178 339q14 16 0 32m22 -32q-14 16 0 32" fill="none"/></g><path d="M0 158H189V484" fill="none" stroke="red" stroke-width="5"/><path d="M189 158h109" stroke="#3f7eba" stroke-width="5"/><path d="M298 158h81" stroke="#f68132" stroke-width="5"/><g fill="white" stroke="#333" stroke-width="1.5"><path d="M154 498v58c0 23 70 23 70 0v-58"/><ellipse cx="189" cy="498" rx="35" ry="16"/></g><text x="189" y="544" text-anchor="middle" font-family="Arial" font-size="12" fill="#17324D">Lung</text></svg>'''
    items[21]['src'] = 'data:image/svg+xml;base64,' + base64.b64encode(svg.encode()).decode()
    items[21]['alt'] = 'Vector single-lung pneumatic route (redrawn from original)'
    css = soup.style.string.replace('84.1000cm;height:118.9000cm','118.9000cm;height:84.1000cm').replace('841mm 1189mm','1189mm 841mm').replace('width:841mm;height:1189mm','width:1189mm;height:841mm').replace('width:841mm!important;height:1189mm!important','width:1189mm!important;height:841mm!important')
    soup.style.string = css
    soup.title.string = 'LungLogic A0 Landscape Poster'
    soup.select_one('.note').string = 'A0 landscape: 118.9 × 84.1 cm. Print at 100% with headers and footers disabled.'
    html = HERE / f'{STEM}.html'
    html.write_text(str(soup), encoding='utf-8')
    # Check every original element retains its original font size.
    for before, after in zip(BeautifulSoup(original,'html.parser').select('.poster > *'), items):
        assert re.findall(r'font-size:([^;]+)',before.get('style','')) == re.findall(r'font-size:([^;]+)',after.get('style',''))
    with tempfile.TemporaryDirectory(prefix='lunglogic_landscape_') as profile:
        subprocess.run([r'C:\Program Files\Google\Chrome\Application\chrome.exe','--headless','--disable-gpu','--no-pdf-header-footer','--no-first-run','--disable-extensions',f'--user-data-dir={profile}',f'--print-to-pdf={HERE / (STEM + ".pdf")}',html.as_uri()],check=True,timeout=90,capture_output=True)
    pdf_path = HERE / f'{STEM}.pdf'
    with fitz.open(pdf_path) as pdf:
        pdf[0].set_mediabox(fitz.Rect(0,0,1189/25.4*72,841/25.4*72))
        pdf.saveIncr()
    with fitz.open(pdf_path) as pdf:
        assert len(pdf)==1
        page=pdf[0]
        print('PDF dimensions (mm):',tuple(round(x/72*25.4,2) for x in (page.rect.width,page.rect.height)))
        page.get_pixmap(dpi=45,alpha=False).save(HERE / f'{STEM}_preview.png')
        page.get_pixmap(dpi=300,alpha=False).save(HERE / f'{STEM}_300dpi.png')
        image_report=[]
        for i,e in enumerate(items):
            if e.name != 'img' or 'svg+xml' in e['src']: continue
            im=Image.open(BytesIO(base64.b64decode(e['src'].split(',')[1])))
            w=float(re.search(r'width:([\d.]+)cm',e['style'])[1])
            h=float(re.search(r'height:([\d.]+)cm',e['style'])[1])
            dpi=max(im.width/w*2.54,im.height/h*2.54)
            if i not in (47,48): assert dpi >= 300, (e.get('alt'),dpi)
            image_report.append({'image':e.get('alt'),'pixels':im.size,'effective_dpi':round(dpi,1),'meets_300dpi':dpi>=300})
        report={'page_mm':[1189,841],'orientation':'landscape','pages':len(pdf),'original_font_sizes_preserved':True,'discipline':'Mechatronics Engineering (confirmed by user)','industry_logo_permission':'confirmed by user','raster_images':image_report,'vector_route':'redrawn from original','300dpi_png_pixels':[round(page.rect.width/72*300),round(page.rect.height/72*300)]}
        (HERE / f'{STEM}_print_checks.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print('300 dpi export saved; all original HTML font sizes preserved.')

if __name__ == '__main__': main()
