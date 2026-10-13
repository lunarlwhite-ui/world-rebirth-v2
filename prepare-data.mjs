import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Rebuild the embedded snapshot from the downloaded public source files.
const root = path.dirname(fileURLToPath(import.meta.url));
const dataDir = path.join(root, 'data');
const read = name => JSON.parse(fs.readFileSync(path.join(dataDir, name), 'utf8').replace(/^\uFEFF/, ''));
const countrySource = read('countries.json');
const gdpSource = read('gdp.json');
const populationSource = read('population.json');
const geography = read('natural-earth-countries.geojson');
const year = '2024';

// UN membership defines eligibility; administrative or territorial status is
// not inferred from GDP data, map polygons, or regional groupings.
const unMembers = new Set(`
AFG ALB DZA AND AGO ATG ARG ARM AUS AUT AZE BHS BHR BGD BRB BLR BEL BLZ BEN BTN
BOL BIH BWA BRA BRN BGR BFA BDI CPV KHM CMR CAN CAF TCD CHL CHN COL COM COG COD
CRI CIV HRV CUB CYP CZE DNK DJI DMA DOM ECU EGY SLV GNQ ERI EST SWZ ETH FJI FIN
FRA GAB GMB GEO DEU GHA GRC GRD GTM GIN GNB GUY HTI HND HUN ISL IND IDN IRN IRQ
IRL ISR ITA JAM JPN JOR KAZ KEN KIR PRK KOR KWT KGZ LAO LVA LBN LSO LBR LBY LIE
LTU LUX MDG MWI MYS MDV MLI MLT MHL MRT MUS MEX FSM MDA MCO MNG MNE MAR MOZ MMR
NAM NRU NPL NLD NZL NIC NER NGA MKD NOR OMN PAK PLW PAN PNG PRY PER PHL POL PRT
QAT ROU RUS RWA KNA LCA VCT WSM SMR STP SAU SEN SRB SYC SLE SGP SVK SVN SLB SOM
ZAF SSD ESP LKA SDN SUR SWE CHE SYR TJK THA TLS TGO TON TTO TUN TUR TKM TUV UGA
UKR ARE GBR TZA USA URY UZB VUT VEN VNM YEM ZMB ZWE
`.trim().split(/\s+/));
const names = new Intl.DisplayNames(['zh-CN'], { type: 'region' });
const shortNames = {
 COD: '刚果民主共和国', COG: '刚果共和国', FSM: '密克罗尼西亚联邦',
 KOR: '韩国', PRK: '朝鲜', LAO: '老挝', BRN: '文莱', TZA: '坦桑尼亚',
 USA: '美国', GBR: '英国', IRN: '伊朗', RUS: '俄罗斯', TUR: '土耳其',
 CIV: '科特迪瓦', TLS: '东帝汶', CPV: '佛得角'
};
const gdp = new Map(gdpSource[1].filter(r => r.date === year).map(r => [r.countryiso3code, r.value]));
const population = new Map(populationSource[1].filter(r => r.date === year).map(r => [r.countryiso3code, r.value]));
const countries = [];
const excluded = [];
for (const c of countrySource[1]) {
 if (!unMembers.has(c.id)) continue;
 const name = shortNames[c.id] || names.of(c.iso2Code);
 if (!(gdp.get(c.id) > 0 && population.get(c.id) > 0)) {
  excluded.push({ id: c.id, name, population: population.get(c.id), reason: '缺少同年人口或人均GDP数据' });
  continue;
 }
 if (c.longitude === '' || c.latitude === '' || !Number.isFinite(Number(c.longitude)) || !Number.isFinite(Number(c.latitude))) {
  throw new Error('Missing reference coordinates for ' + c.id);
 }
 countries.push({
  id: c.id, iso2: c.iso2Code, name, nameEn: c.name, region: c.region.id,
  gdp: gdp.get(c.id), population: population.get(c.id),
  longitude: Number(c.longitude), latitude: Number(c.latitude), capital: c.capitalCity
 });
}
countries.sort((a, b) => a.gdp - b.gdp || a.id.localeCompare(b.id));
const coveredPopulation = countries.reduce((sum, c) => sum + c.population, 0);
const worldPopulation = population.get('WLD');

const squaredDistance = (p, a, b) => {
 let x = a[0], y = a[1], dx = b[0] - x, dy = b[1] - y;
 if (dx !== 0 || dy !== 0) {
  const t = ((p[0] - x) * dx + (p[1] - y) * dy) / (dx * dx + dy * dy);
  if (t > 1) { x = b[0]; y = b[1]; }
  else if (t > 0) { x += dx * t; y += dy * t; }
 }
 dx = p[0] - x; dy = p[1] - y;
 return dx * dx + dy * dy;
};
function simplify(points, first, last, tolerance, output) {
 let furthest = tolerance, pivot = 0;
 for (let i = first + 1; i < last; i++) {
  const distance = squaredDistance(points[i], points[first], points[last]);
  if (distance > furthest) { furthest = distance; pivot = i; }
 }
 if (furthest > tolerance) {
  if (pivot - first > 1) simplify(points, first, pivot, tolerance, output);
  output.push(points[pivot]);
  if (last - pivot > 1) simplify(points, pivot, last, tolerance, output);
 }
}
function ringPath(ring) {
 let points = ring;
 if (ring.length > 12) {
  const reduced = [ring[0]];
  simplify(ring, 0, ring.length - 1, .035 ** 2, reduced);
  reduced.push(ring[ring.length - 1]);
  if (reduced.length >= 4) points = reduced;
 }
 return points.map((p, i) => (i ? 'L' : 'M') + Number(((p[0] + 180) / 360 * 1000).toFixed(2)) + ',' + Number(((90 - p[1]) / 180 * 500 + 15).toFixed(2))).join('') + 'Z';
}
const landPaths = [];
for (const feature of geography.features) {
 const p = feature.properties;
 if (p.ADM0_A3 === 'ATA' || p.ISO_A3 === 'ATA') continue;
 const polygons = feature.geometry.type === 'Polygon' ? [feature.geometry.coordinates] : feature.geometry.type === 'MultiPolygon' ? feature.geometry.coordinates : [];
 for (const polygon of polygons) for (const ring of polygon) landPaths.push(ringPath(ring));
}
const metadata = {
 year: Number(year), downloadDate: '2026-10-13', sourceUpdated: gdpSource[0].lastupdated,
 source: 'World Bank, World Development Indicators', gdpIndicator: 'NY.GDP.PCAP.CD', populationIndicator: 'SP.POP.TOTL',
 eligibility: 'UN member states with positive GDP per capita and population in the same year',
 unMemberCount: unMembers.size, countryCount: countries.length, coveredPopulation, worldPopulation,
 coverage: coveredPopulation / worldPopulation,
 membershipSource: 'https://www.un.org/en/about-us/member-states',
 gdpSource: 'https://data.worldbank.org/indicator/NY.GDP.PCAP.CD',
 populationSource: 'https://data.worldbank.org/indicator/SP.POP.TOTL',
 mapSource: 'https://github.com/nvkelso/natural-earth-vector/tree/v5.1.2/geojson',
 mapLicense: 'Public domain', dataLicense: 'CC BY 4.0'
};
const snapshot = { meta: metadata, countries, excluded };
fs.writeFileSync(path.join(dataDir, 'dataset.json'), JSON.stringify(snapshot, null, 2), 'utf8');
const embedded = JSON.stringify({ ...snapshot, land: landPaths.join('') }).replace(/</g, '\\u003c');
const htmlPath = path.join(root, 'index.html');
const html = fs.readFileSync(htmlPath, 'utf8');
const start = '<script id="embedded-data" type="application/json">';
const startIndex = html.indexOf(start);
const endIndex = html.indexOf('</script>', startIndex + start.length);
if (startIndex < 0 || endIndex < 0) throw new Error('Embedded data container is missing from index.html');
fs.writeFileSync(htmlPath, html.slice(0, startIndex + start.length) + embedded + html.slice(endIndex), 'utf8');
console.log(JSON.stringify({ file: htmlPath, countries: countries.length, unMembers: unMembers.size, population: coveredPopulation, coverage: metadata.coverage, excluded: excluded.map(c => c.id) }));
