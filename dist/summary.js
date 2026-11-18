const endpoint = "https://www.ebi.ac.uk/europepmc/webservices/rest/search";
const segmenter = typeof Intl.Segmenter === "function"
  ? new Intl.Segmenter("en-AU", { granularity: "sentence" })
  : null;

function queryFor(article) {
  const info = article.metadata || {};
  const match = (info.abstractUrl || "").match(/^https:\/\/europepmc\.org\/article\/MED\/(\d+)$/);
  if (match) return { query: `EXT_ID:${match[1]} AND SRC:MED`, pmid: match[1] };
  const pmc = (info.abstractUrl || "").match(/^https:\/\/europepmc\.org\/articles\/(PMC\d+)$/);
  if (pmc) return { query: `PMC_ID:${pmc[1]}`, pmcid: pmc[1] };
  const doi = info.doi || article.doi;
  if (doi && /^10\.\d{4,9}\/[^"\s]+$/.test(doi)) return { query: `DOI:"${doi}"`, doi };
  return null;
}

export function canLoadSummary(article) {
  return Boolean(queryFor(article));
}

function sentences(text) {
  if (segmenter) return Array.from(segmenter.segment(text), ({ segment }) => segment.trim());
  return text.split(/(?<=[.!?])\s+(?=[A-Z])/);
}

function highlights(html) {
  const template = document.createElement("template");
  template.innerHTML = html;
  const fragment = template.content;
  fragment.querySelectorAll("script, style").forEach((node) => node.remove());
  const headings = [...fragment.querySelectorAll("h2,h3,h4,h5")];
  const sections = headings.map((heading, index) => {
    const range = document.createRange();
    range.setStartAfter(heading);
    if (headings[index + 1]) range.setEndBefore(headings[index + 1]);
    else range.setEnd(fragment, fragment.childNodes.length);
    return [heading.textContent.trim(), range.toString().replace(/\s+/g, " ").trim()];
  });
  const groups = [
    ["Study aim", /objective|purpose|\baim/i, 1],
    ["Methods", /method|design|setting|participant|intervention/i, 1],
    ["Results", /result|finding/i, 2],
    ["Authors’ conclusion", /conclu|interpret/i, 1],
  ];
  const selected = groups.flatMap(([label, pattern, limit]) => {
    const section = sections.find(([heading, text]) => text && pattern.test(heading));
    return section ? [[label, sentences(section[1]).slice(0, limit).join(" ")]] : [];
  });
  if (selected.some(([label]) => label === "Results" || label === "Authors’ conclusion")) return selected;
  const text = fragment.textContent.replace(/\s+/g, " ").trim();
  if (!text) return [];
  const parts = sentences(text);
  const indexes = new Set([0, Math.max(0, parts.length - 2), parts.length - 1]);
  return [["", [...indexes].map((index) => parts[index]).join(" ")]];
}

export async function loadSummary(article, signal) {
  const lookup = queryFor(article);
  if (!lookup) return [];
  const url = new URL(endpoint);
  url.search = new URLSearchParams({ query: lookup.query, resultType: "core", format: "json", pageSize: 10 });
  const response = await fetch(url, { credentials: "omit", signal });
  if (!response.ok) throw new Error("Europe PMC unavailable");
  const data = await response.json();
  const record = data.resultList?.result?.find((row) => row.abstractText && (
    (lookup.pmid && row.pmid === lookup.pmid)
    || (lookup.pmcid && row.pmcid === lookup.pmcid)
    || (lookup.doi && row.doi?.toLowerCase() === lookup.doi.toLowerCase())
  ));
  return record ? highlights(record.abstractText) : [];
}
