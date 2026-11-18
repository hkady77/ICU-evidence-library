#!/usr/bin/env python3
"""Import English-language PubMed records from Europe PMC for a rolling three months."""
import argparse
from datetime import date, datetime
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from refresh import TOPICS, clean, cutoff, key_for, session, verified_pdf

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'dist/evidence.json'
API = 'https://www.ebi.ac.uk/europepmc/webservices/rest/search'
TERMS = [
    'intensive care', 'critical care', 'critically ill', 'septic shock',
    'acute respiratory distress syndrome', 'mechanical ventilation', 'cardiac arrest',
]
TYPE_RULES = [
    ('Randomized Controlled Trial', 'Randomised trial'),
    ('Meta-Analysis', 'Systematic review / meta-analysis'),
    ('Systematic Review', 'Systematic review / meta-analysis'),
    ('Practice Guideline', 'Guideline / consensus'),
    ('Guideline', 'Guideline / consensus'),
    ('Clinical Trial Protocol', 'Protocol'),
    ('Observational Study', 'Observational study'),
    ('Clinical Trial', 'Clinical trial'),
    ('Review', 'Review'),
    ('Editorial', 'Editorial'),
    ('Comment', 'Commentary'),
    ('Letter', 'Correspondence'),
    ('Case Reports', 'Case report'),
]


def study_type(record):
    published = record.get('pubTypeList', {}).get('pubType', [])
    for name, label in TYPE_RULES:
        if name.lower() in {value.lower() for value in published}:
            return label
    return 'Other research'


def query_for(start, end):
    topics = ' OR '.join(f'TITLE_ABS:"{term}"' for term in TERMS)
    return (f'({topics}) AND FIRST_PDATE:[{start} TO {end}] '
            'AND SRC:MED AND LANG:eng')


def transform(record, start, end):
    published = record.get('firstPublicationDate', '')
    pmid = record.get('pmid') or record.get('id', '')
    if not start <= published <= end or not re.fullmatch(r'\d+', pmid):
        return None
    title = clean(record.get('title') or '').rstrip('.')
    if not title:
        return None
    doi = (record.get('doi') or '').lower()
    journal = record.get('journalInfo', {}).get('journal', {}).get('medlineAbbreviation', '')
    journal = journal or record.get('journalInfo', {}).get('journal', {}).get('title', '')
    abstract_url = f'https://europepmc.org/article/MED/{pmid}'
    url = 'https://doi.org/' + doi if doi else abstract_url
    pdf, full_text = verified_pdf(record)
    mesh = ' '.join(item.get('descriptorName', '') for item in record.get('meshHeadingList', {}).get('meshHeading', []))
    labels = [name for name, pattern in TOPICS if pattern.search(title + ' ' + mesh)]
    citation = '. '.join(filter(None, [record.get('authorString', ''), title, journal, published[:4]]))
    return dict(
        id=key_for(dict(doi=doi, title=title)), title=title, journal=journal,
        citation=citation, url=url, doi=doi, date=published, type=study_type(record),
        sourceType=', '.join(record.get('pubTypeList', {}).get('pubType', [])),
        topics=labels or ['General critical care'],
        metadata=dict(
            title=title, doi=doi, journal=journal, pmid=pmid,
            abstractUrl=abstract_url, hasAbstract=bool(record.get('abstractText')),
            pdf=pdf, fullText=full_text, openAccess=record.get('isOpenAccess') == 'Y',
        ),
    )


def refresh(today):
    start = cutoff(today).isoformat()
    end = today.isoformat()
    query = query_for(start, end)
    cursor = '*'
    retrieved = 0
    records = {}
    with session() as client:
        while True:
            response = client.get(API, params=dict(
                query=query, format='json', resultType='core', pageSize=500,
                cursorMark=cursor, sort='FIRST_PDATE_D desc',
            ), timeout=60)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload.get('hitCount'), int):
                raise RuntimeError('Europe PMC returned an unexpected response. Existing index preserved.')
            batch = payload.get('resultList', {}).get('result', [])
            if not batch:
                if retrieved < payload['hitCount']:
                    raise RuntimeError('Europe PMC returned incomplete results. Existing index preserved.')
                break
            retrieved += len(batch)
            for record in batch:
                row = transform(record, start, end)
                if row:
                    records.setdefault(row['doi'] or row['metadata']['pmid'], row)
            print(f'Read {retrieved} of {payload["hitCount"]} records', flush=True)
            if retrieved >= payload['hitCount']:
                break
            next_cursor = payload.get('nextCursorMark')
            if not next_cursor or next_cursor == cursor:
                raise RuntimeError('Europe PMC pagination stopped early. Existing index preserved.')
            cursor = next_cursor
    if not records:
        raise RuntimeError('No articles returned. Existing index preserved.')
    articles = sorted(records.values(), key=lambda row: (row['date'], row['title']), reverse=True)
    previous = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else {}
    previous_ids = {row['id'] for row in previous.get('articles', [])}
    data = dict(
        source='https://europepmc.org/', provider='Europe PMC',
        dateBasis='First publication date', searchQuery=query, searchTerms=TERMS,
        windowStart=start, windowEnd=end, latest=max(row['date'] for row in articles),
        updated=datetime.now(ZoneInfo('Australia/Sydney')).isoformat(timespec='seconds'),
        topics=[name for name, _ in TOPICS] + ['General critical care'], articles=articles,
    )
    comparable_previous = {key: value for key, value in previous.items() if key != 'updated'}
    comparable_new = {key: value for key, value in data.items() if key != 'updated'}
    if comparable_previous == comparable_new:
        print('No changes. Existing snapshot retained.')
        return
    temporary = OUTPUT.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n')
    temporary.replace(OUTPUT)
    print(f'Saved {len(articles)} articles, {sum(bool(row["metadata"]["hasAbstract"]) for row in articles)} abstract records, '
          f'{sum(bool(row["metadata"]["pdf"]) for row in articles)} PDF links. '
          f'Latest publication {data["latest"]}. '
          f'{sum(row["id"] not in previous_ids for row in articles)} additions.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--as-of', type=date.fromisoformat)
    args = parser.parse_args()
    refresh(args.as_of or datetime.now(ZoneInfo('Australia/Sydney')).date())
