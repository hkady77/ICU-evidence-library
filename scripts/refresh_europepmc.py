#!/usr/bin/env python3
"""Import English-language PubMed records from Europe PMC for a rolling three months."""
import argparse
import hashlib
from html import unescape
from datetime import date, datetime
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from refresh import TOPICS, clean, cutoff, key_for, session, verified_pdf

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'dist/evidence.json'
JOURNAL_FILE = ROOT / 'scripts/journals.json'
MANIFEST = ROOT / 'dist/version.json'
API = 'https://www.ebi.ac.uk/europepmc/webservices/rest/search'
TERMS = [
    'intensive care', 'critical care', 'critically ill', 'septic shock',
    'acute respiratory distress syndrome', 'mechanical ventilation', 'cardiac arrest',
]
ADULT_AGES = {'adult', 'young adult', 'middle aged', 'aged', 'aged 80 and over'}
CHILD_AGES = {'adolescent', 'child', 'child preschool', 'infant', 'infant newborn'}
CHILD_TITLE = re.compile(
    r'\b(?:pa?ediatric\w*|neonat\w*|newborns?|infants?|children|child|'
    r'babies|baby|adolescen\w*|teenagers?|teens?|PICUs?|NICUs?)\b', re.I)
ADULT_TITLE = re.compile(
    r'\b(?:adults?|middle[ -]aged|older (?:people|patients|persons)|elderly|'
    r'parents?|caregivers?|mothers?|fathers?|maternal|pregnan\w*)\b', re.I)
POPULATION_POLICY = {
    'label': 'Adult-focused',
    'version': 1,
    'description': 'Excludes records with child/adolescent/infant age indexing but no adult age indexing, '
                   'or explicit paediatric/neonatal title wording. Adult age indexing or adult/mixed-age '
                   'title context takes precedence. Mixed-age and age-unclear records are retained. '
                   'This is a screening heuristic, not confirmation of adult-only eligibility.',
}


def population_exclusion(record, title):
    """Conservative screening: retain adult, mixed-age and age-unclear records."""
    headings = record.get('meshHeadingList', {}).get('meshHeading', [])
    ages = {re.sub(r'[^a-z0-9]+', ' ', item.get('descriptorName', '').lower()).strip()
            for item in headings}
    # Child tags can coexist with adult tags or describe children of adult participants.
    if ages & ADULT_AGES or ADULT_TITLE.search(title):
        return None
    if ages & CHILD_AGES:
        return 'child_age_indexing_without_adult_context'
    if CHILD_TITLE.search(title):
        return 'paediatric_or_neonatal_title_without_adult_context'
    return None


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
    selection = json.loads(JOURNAL_FILE.read_text())
    issns = sorted({issn for journal in selection['journals'] for issn in journal['issns']})
    if not issns or any(not re.fullmatch(r'[0-9]{4}-[0-9]{3}[0-9X]', value) for value in issns):
        raise ValueError('Journal shortlist must contain valid ISSNs.')
    journals = ' OR '.join(f'ISSN:{issn}' for issn in issns)
    return (f'({topics}) AND ({journals}) AND FIRST_PDATE:[{start} TO {end}] '
            'AND SRC:MED AND LANG:eng')


def transform(record, start, end):
    published = record.get('firstPublicationDate', '')
    pmid = record.get('pmid') or record.get('id', '')
    if not start <= published <= end or not re.fullmatch(r'\d+', pmid):
        return None
    from bs4 import BeautifulSoup
    raw_title = record.get('title') or ''
    title = clean(BeautifulSoup(raw_title, 'html.parser').get_text(' ') if '<' in raw_title else unescape(raw_title)).rstrip('.')
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
            pmid=pmid,
            abstractUrl=abstract_url, hasAbstract=bool(record.get('abstractText')),
            pdf=pdf, fullText=full_text, openAccess=record.get('isOpenAccess') == 'Y',
        ),
    )


def write_manifest(data):
    manifest = dict(version=hashlib.sha256(OUTPUT.read_bytes()).hexdigest(),
                    updated=data['updated'], articleCount=len(data['articles']))
    content = json.dumps(manifest, separators=(',', ':')) + '\n'
    if MANIFEST.exists() and MANIFEST.read_text() == content:
        return
    temporary = MANIFEST.with_suffix('.tmp')
    temporary.write_text(content)
    temporary.replace(MANIFEST)


def refresh(today):
    start = cutoff(today).isoformat()
    end = today.isoformat()
    query = query_for(start, end)
    cursor = '*'
    retrieved = 0
    records = {}
    population_excluded = {}
    selection = json.loads(JOURNAL_FILE.read_text())
    allowed = {re.sub(r'[^a-z0-9]', '', j['abbreviation'].lower()) for j in selection['journals']}
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
                journal = record.get('journalInfo', {}).get('journal', {}).get('medlineAbbreviation', '')
                if re.sub(r'[^a-z0-9]', '', journal.lower()) not in allowed:
                    continue
                row = transform(record, start, end)
                if row:
                    reason = population_exclusion(record, row['title'])
                    if reason:
                        population_excluded[reason] = population_excluded.get(reason, 0) + 1
                        continue
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
    selection = json.loads(JOURNAL_FILE.read_text())
    previous_ids = {row['id'] for row in previous.get('articles', [])}
    data = dict(
        source='https://europepmc.org/', provider='Europe PMC',
        dateBasis='First publication date', searchQuery=query, searchTerms=TERMS,
        journalSelection=selection,
        populationFilter=dict(POPULATION_POLICY, excludedCounts=population_excluded),
        windowStart=start, windowEnd=end, latest=max(row['date'] for row in articles),
        updated=datetime.now(ZoneInfo('Australia/Sydney')).isoformat(timespec='seconds'),
        topics=[name for name, _ in TOPICS] + ['General critical care'], articles=articles,
    )
    comparable_previous = {key: value for key, value in previous.items() if key != 'updated'}
    print(f'Adult-focused screening excluded {sum(population_excluded.values())} records: '
          f'{json.dumps(population_excluded, sort_keys=True)}', flush=True)
    comparable_new = {key: value for key, value in data.items() if key != 'updated'}
    if comparable_previous == comparable_new:
        write_manifest(previous)
        print('No changes. Existing snapshot retained.')
        return
    temporary = OUTPUT.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n')
    temporary.replace(OUTPUT)
    write_manifest(data)
    print(f'Saved {len(articles)} articles, {sum(bool(row["metadata"]["hasAbstract"]) for row in articles)} abstract records, '
          f'{sum(bool(row["metadata"]["pdf"]) for row in articles)} PDF links. '
          f'Latest publication {data["latest"]}. '
          f'{sum(row["id"] not in previous_ids for row in articles)} additions.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--as-of', type=date.fromisoformat)
    args = parser.parse_args()
    refresh(args.as_of or datetime.now(ZoneInfo('Australia/Sydney')).date())
