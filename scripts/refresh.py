#!/usr/bin/env python3
"""Refresh the private Journal Watch index and enrich verified bibliographic matches."""
import argparse
import calendar
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'dist' / 'evidence.json'
SOURCE = 'https://criticalcarereviews.com/latest-evidence/journal-watch'
EUROPE_PMC = 'https://www.ebi.ac.uk/europepmc/webservices/rest/search'
TOPICS = [(name, re.compile(pattern, re.I))
          for name, pattern in json.loads((ROOT / 'scripts/topics.json').read_text())]
MONTHS = {month.lower(): number for number, month in enumerate(calendar.month_name) if month}
WEEKDAYS = {day.lower(): number for number, day in enumerate(calendar.day_name)}
DAY = re.compile(r'(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+'
                 r'(?:([a-z]+)\s+)?(\d{1,2})(?:st|nd|rd|th)?(?:\s+([a-z]+))?\.?$', re.I)
DOI = re.compile(r'10\.\d{4,9}/[^?#\s]+', re.I)
CITATION = re.compile(r'[.?!]\s+(?=[^.?!;]{2,115}?\.?\s+(?:\(?20\d{2}\)?[;:., ]|epublished\b))')
ARTICLE_TYPES = [
    (r'randomi[sz]ed.*(?:control|trial)', 'Randomised trial'),
    (r'systematic|meta.analysis', 'Systematic review / meta-analysis'),
    (r'observation', 'Observational study'),
    (r'protocol', 'Protocol'),
    (r'guideline|position statement|consensus', 'Guideline / consensus'),
    (r'narrative|^reviews$', 'Narrative review'),
    (r'editorial', 'Editorial'),
    (r'commentar', 'Commentary'),
    (r'correspondence', 'Correspondence'),
    (r'case report', 'Case report'),
    (r'research|clinical', 'Other research'),
]


def clean(value):
    return ' '.join(value.split())


def normalized(value):
    value = unicodedata.normalize('NFKD', value.lower())
    return ' '.join(re.findall(r'[a-z0-9]+', ''.join(c for c in value if not unicodedata.combining(c))))


def cutoff(today):
    year, month = divmod(today.year * 12 + today.month - 4, 12)
    return date(year, month + 1, min(today.day, calendar.monthrange(year, month + 1)[1]))


def article_type(section, subsection):
    for label in (subsection, section):
        for pattern, kind in ARTICLE_TYPES:
            if re.search(pattern, label, re.I):
                return kind
    return 'Other'


def parse_citation(citation):
    body = re.sub(r"^[^.!?:]{1,38}\.\s+(?=[\w\u201c])", '', citation, count=1)
    split = list(CITATION.finditer(body))
    if split:
        match = split[-1]
        title = body[:match.start() + int(body[match.start()] != '.')].strip()
        journal = re.split(r'\s+\(?20\d{2}\b|\s+epublished\b', body[match.end():], maxsplit=1)[0]
        return title, journal.strip(' .,;')
    return body.rstrip('.'), ''


def parse_source(html, start, end):
    soup = BeautifulSoup(html, 'html.parser')
    heading = soup.find('h1', string=re.compile(r'Journal Watch\s+20\d{2}', re.I))
    if not heading:
        heading = next((h for h in soup.find_all('h1') if re.search(r'Journal Watch\s+20\d{2}', h.get_text(' ', strip=True), re.I)), None)
    if not heading:
        raise ValueError('Journal Watch heading is missing')
    year = int(re.search(r'20\d{2}', heading.get_text(' ', strip=True)).group())
    entries, month_count = [], 0
    for month_heading in soup.find_all('h2', id=True):
        month = MONTHS.get(month_heading.get_text(' ', strip=True).lower())
        if not month:
            continue
        block = month_heading.find_next_sibling()
        if not block:
            continue
        panels = block.select('.uk-switcher .el-content')
        if not panels:
            continue
        month_count += 1
        month_start = date(year, month, 1)
        month_end = date(year, month, calendar.monthrange(year, month)[1])
        lower, upper = month_start - timedelta(days=7), month_end + timedelta(days=7)
        for panel in panels:
            current, section, subsection = None, '', ''
            for element in panel.find_all(['h2', 'h3', 'h4', 'h5', 'li', 'p']):
                if element.name.startswith('h'):
                    label = clean(element.get_text(' ', strip=True))
                    if not label:
                        continue
                    day = DAY.fullmatch(label)
                    if day:
                        weekday, prefix, number, suffix = day.groups()
                        explicit_month = MONTHS.get((prefix or suffix or '').lower())
                        dates = [lower + timedelta(days=i) for i in range((upper - lower).days + 1)]
                        candidates = [d for d in dates if d.day == int(number)
                                      and d.weekday() == WEEKDAYS[weekday.lower()]
                                      and (explicit_month is None or d.month == explicit_month)]
                        if not candidates:
                            candidates = [d for d in dates if d.day == int(number)
                                          and d.month == (explicit_month or month)]
                        current = min(candidates, key=lambda d: abs(d - month_start) + abs(d - month_end), default=None)
                        section, subsection = '', ''
                    elif element.name in ('h2', 'h3'):
                        section, subsection = label, ''
                    else:
                        if article_type(label, '') not in {'Other', 'Other research'} or not subsection:
                            subsection = label
                    continue
                if not current or not start <= current <= end:
                    continue
                if element.name == 'p' and element.find_parent('li'):
                    continue
                for link in element.find_all('a', href=True):
                    if link.find_parent(['li', 'p']) != element:
                        continue
                    url = urljoin(SOURCE, link['href'])
                    parts = urlsplit(url)
                    query = urlencode([(key, value) for key, value in parse_qsl(parts.query)
                                       if not key.lower().startswith('utm_') and key.lower() not in
                                       {'ssotoken', 'guestaccesskey', 'dgcid', 'cid', 'bid', 'query'}])
                    url = parts._replace(query=query).geturl()
                    citation = clean(link.get_text(' ', strip=True))
                    if urlsplit(url).scheme != 'https' or len(citation) < 24:
                        continue
                    if urlsplit(url).hostname in {'criticalcarereviews.com', 'www.criticalcarereviews.com'}:
                        continue
                    if not re.search(r'\b20\d{2}\b', citation) and not DOI.search(unquote(url)):
                        continue
                    title, journal = parse_citation(citation)
                    match = DOI.search(unquote(url))
                    doi = match.group().rstrip('.,;').lower() if match else ''
                    entries.append(dict(title=title, journal=journal, citation=citation, url=url,
                                        doi=doi, date=current.isoformat(),
                                        type=article_type(section, subsection),
                                        sourceType=subsection or section or 'Other'))
    if not month_count:
        raise ValueError('No monthly article lists found')
    return entries


def session():
    client = requests.Session()
    retry = Retry(total=2, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))
    client.mount('https://', HTTPAdapter(max_retries=retry))
    return client


def fetch_source(client, url, start, end):
    failures = []
    for candidate in (url, url + '?tmpl=component', url.replace('https://', 'https://www.', 1)):
        try:
            response = client.get(candidate, headers={'Cache-Control': 'no-cache'}, timeout=30)
            response.raise_for_status()
            return parse_source(response.text, start, end)
        except (requests.RequestException, ValueError) as error:
            failures.append(str(error))
    raise RuntimeError(f'Journal Watch fetch failed: {failures[-1]}')


def key_for(row):
    # Preserve identifiers across enrichment and publisher URL changes for DOI records.
    value = row['doi'] or normalized(row['title'])
    return hashlib.sha256(value.encode()).hexdigest()[:20]


def verified_pdf(record):
    if record.get('isOpenAccess') != 'Y':
        return '', ''
    links = record.get('fullTextUrlList', {}).get('fullTextUrl', [])
    for link in links:
        url = link.get('url', '')
        if (url.startswith('https://') and link.get('documentStyle', '').lower() == 'pdf'
                and link.get('availabilityCode') == 'OA'):
            return url, ''
    pmc = record.get('pmcid', '')
    if re.fullmatch(r'PMC\d+', pmc):
        article = 'https://europepmc.org/articles/' + pmc
        if record.get('hasPDF') == 'Y':
            return article + '?pdf=render', article
        return '', article
    return '', ''


def abstract_sections(record):
    soup = BeautifulSoup(record.get('abstractText') or '', 'html.parser')
    for node in soup(['script', 'style']):
        node.decompose()
    heading_tags = ['h2', 'h3', 'h4', 'h5', 'strong', 'b']
    sections, heading, parts = [], '', []
    headings = soup.find_all(heading_tags)
    if headings:
        for node in soup.descendants:
            if getattr(node, 'name', None) in heading_tags and node.find_parent(heading_tags) is None:
                if clean(' '.join(parts)):
                    sections.append([heading, clean(' '.join(parts))])
                heading, parts = node.get_text(' ', strip=True).strip(' :.'), []
            elif isinstance(node, str) and node.find_parent(heading_tags) is None:
                parts.append(str(node))
        if clean(' '.join(parts)):
            sections.append([heading, clean(' '.join(parts))])
    else:
        abstract = clean(soup.get_text(' ', strip=True))
        if abstract:
            sections.append(['', abstract])
    return sections


def match_batch(batch):
    terms = []
    for row in batch:
        if row['doi']:
            terms.append('DOI:"' + row['doi'].replace('"', '') + '"')
        title = re.sub(r'[^\w\s-]', ' ', row['title'])
        if len(title.split()) >= 3:
            terms.append('TITLE_ABS:"' + clean(title) + '"')
    if not terms:
        return {}
    with session() as client:
        response = client.get(EUROPE_PMC, params=dict(query=' OR '.join(terms), resultType='core',
                                                       format='json', pageSize=100), timeout=35)
        response.raise_for_status()
        records = response.json().get('resultList', {}).get('result', [])
    results = {}
    for row in batch:
        title = normalized(row['title'])
        doi = row['doi']
        candidates = [record for record in records
                      if title == normalized(record.get('title') or '')
                      and (not doi or record.get('doi', '').lower() == doi)]
        if len({record.get('doi') for record in candidates if record.get('doi')}) > 1:
            continue
        if candidates:
            record = max(candidates, key=lambda item: (item.get('source') == 'MED', bool(item.get('abstractText'))))
            results[row['id']] = record
    return results


def enrichment(record):
    pdf, full_text = verified_pdf(record)
    pmid = record.get('pmid', '')
    pmc = record.get('pmcid', '')
    abstract_url = (f'https://europepmc.org/article/MED/{pmid}' if pmid else
                    f'https://europepmc.org/articles/{pmc}' if pmc else '')
    return dict(
        title=clean(record.get('title') or '').rstrip('.'),
        doi=record.get('doi', ''), journal=record.get('journalInfo', {}).get('journal', {}).get('medlineAbbreviation', ''),
        abstract=abstract_sections(record), abstractUrl=abstract_url, pdf=pdf, fullText=full_text,
        openAccess=record.get('isOpenAccess') == 'Y',
    )


def springer_metadata(row):
    doi = row['doi'] or row.get('metadata', {}).get('doi', '')
    if not doi.startswith(('10.1186/', '10.1007/', '10.1038/')):
        return {}
    url = 'https://link.springer.com/article/' + doi
    with session() as client:
        response = client.get(url, timeout=12)
        if not response.ok:
            return {}
    soup = BeautifulSoup(response.text, 'html.parser')
    title = soup.find('meta', attrs={'name': 'citation_title'})
    if not title or normalized(title.get('content', '')) != normalized(row['title']):
        return {}
    publisher = {}
    access = soup.select_one('.u-color-open-access')
    pdf = soup.find('meta', attrs={'name': 'citation_pdf_url'})
    if access and clean(access.get_text(' ', strip=True)).lower() == 'open access':
        publisher.update(openAccess=True, fullText=url)
        if pdf and pdf.get('content', '').startswith('https://link.springer.com/content/pdf/'):
            publisher['pdf'] = pdf['content']
    if not row.get('metadata', {}).get('hasAbstract'):
        section = soup.select_one('#Abs1')
        if section and section.find_next_sibling():
            abstract = abstract_sections({'abstractText': str(section.find_next_sibling())})
            if abstract:
                publisher.update(abstract=abstract, abstractUrl=url)
    return publisher


def refresh(today, offline_html=None, enrich=True):
    start = cutoff(today)
    previous = json.loads(DATA.read_text()) if DATA.exists() else {}
    cached = {row['id']: row for row in previous.get('articles', [])}
    if offline_html:
        entries = parse_source(offline_html.read_text(), start, today)
    else:
        with session() as client:
            entries = fetch_source(client, SOURCE, start, today)
            if start.year < today.year:
                entries += fetch_source(client, f'https://criticalcarereviews.com/journal-watch/journal-watch-{start.year}', start, today)
    if not entries:
        raise RuntimeError('No entries in the requested window. Existing data was preserved.')
    latest = max(row['date'] for row in entries)
    if not offline_html and latest < previous.get('latest', ''):
        raise RuntimeError(f'Journal Watch returned entries through {latest}, older than the published snapshot. Existing data was preserved.')
    if not offline_html and date.fromisoformat(latest) < today - timedelta(days=14):
        raise RuntimeError(f'Journal Watch returned a stale page ending {latest}. Existing data was preserved.')
    if len(entries) < len(cached) * 0.8:
        raise RuntimeError('Source returned substantially fewer entries. Existing data was preserved.')
    deduped = {}
    for row in sorted(entries, key=lambda item: item['date'], reverse=True):
        row['id'] = key_for(row)
        deduped.setdefault(row['id'], row)
    pending = []
    for key, row in deduped.items():
        prior = cached.get(key, {})
        prior_info = prior.get('metadata', {})
        same_title = not prior_info.get('title') or normalized(prior_info['title']) == normalized(row['title'])
        if prior_info and same_title:
            row['metadata'] = prior_info
        checked = prior.get('checked', '') if same_title else ''
        if prior.get('publisherChecked'):
            row['publisherChecked'] = prior['publisherChecked']
        if checked:
            row['checked'] = checked
        if not checked or (checked < (today - timedelta(days=7)).isoformat()
                           and not (prior.get('metadata', {}).get('hasAbstract') and prior.get('metadata', {}).get('pdf'))):
            pending.append(row)
    if enrich and pending:
        batches = [pending[i:i + 15] for i in range(0, len(pending), 15)]
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(match_batch, batch) for batch in batches]
            for batch, future in zip(batches, futures):
                try:
                    matches = future.result()
                except (requests.RequestException, ValueError) as error:
                    print(f'Metadata batch unavailable: {error}', file=sys.stderr)
                    continue
                for row in batch:
                    row['checked'] = today.isoformat()
                    if row['id'] in matches:
                        row['metadata'] = enrichment(matches[row['id']])
    if enrich:
        publisher_pending = [row for row in deduped.values()
                             if (row['doi'] or row.get('metadata', {}).get('doi', '')).startswith(('10.1186/', '10.1007/', '10.1038/'))
                             and row.get('publisherChecked', '') < (today - timedelta(days=7)).isoformat()
                             and not row.get('metadata', {}).get('pdf', '').startswith('https://link.springer.com/')]
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(springer_metadata, row) for row in publisher_pending]
            for row, future in zip(publisher_pending, futures):
                try:
                    info = future.result()
                except (requests.RequestException, ValueError):
                    continue
                if info:
                    row.setdefault('metadata', {}).update(info)
                row['publisherChecked'] = today.isoformat()
    articles = []
    canonical_ids = set()
    for row in deduped.values():
        info = row.get('metadata', {})
        if 'abstract' in info:
            info['hasAbstract'] = bool(info.pop('abstract'))
        row['topics'] = [name for name, pattern in TOPICS if pattern.search(info.get('title') or row['title'])]
        if not row['topics']:
            row['topics'] = ['General critical care']
        canonical = (info.get('doi') or row['doi'] or row['id']).lower()
        if canonical not in canonical_ids:
            canonical_ids.add(canonical)
            articles.append(row)
    payload = dict(source=SOURCE, windowStart=start.isoformat(), windowEnd=today.isoformat(),
                   latest=latest, updated=datetime.now(ZoneInfo('Australia/Sydney')).isoformat(timespec='seconds'),
                   topics=[name for name, _ in TOPICS] + ['General critical care'], articles=articles)
    temp = DATA.with_suffix('.tmp')
    temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n')
    temp.replace(DATA)
    print(f'{len(articles)} articles from {start} through {latest}. '
          f'{sum(bool(a.get("metadata", {}).get("hasAbstract")) for a in articles)} abstract records, '
          f'{sum(bool(a.get("metadata", {}).get("pdf")) for a in articles)} PDF links.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--as-of', type=date.fromisoformat)
    parser.add_argument('--source-html', type=Path)
    parser.add_argument('--skip-enrichment', action='store_true')
    args = parser.parse_args()
    refresh(args.as_of or datetime.now(ZoneInfo('Australia/Sydney')).date(), args.source_html, not args.skip_enrichment)
