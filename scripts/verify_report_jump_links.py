import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
from app.database import get_mongo_db
from app.tools.report_renderer import ReportRendererTool

async def verify():
    db = await get_mongo_db()
    run = await db['runs'].find_one({'_id': 'run_e6426e8d5338'})
    if not run:
        run = await db['runs'].find_one(sort=[('created_at', -1)])
    if not run:
        print('Run not found in database')
        return
    rep = run.get('final_report', {})
    html = ReportRendererTool.render_html(rep)
    with open('reports/report_run_e6426e8d5338.html', 'w', encoding='utf-8') as f:
        f.write(html)
    print('HTML Report length:', len(html))
    print('Jump links <a href="#tc-..."> present:', '#tc-' in html)
    print('Target row <tr id="tc-..."> present:', 'id="tc-' in html)
    print('Smooth scroll CSS present:', 'scroll-behavior: smooth' in html)
    print('Highlight animation CSS present:', 'highlightTarget' in html)

if __name__ == '__main__':
    asyncio.run(verify())
