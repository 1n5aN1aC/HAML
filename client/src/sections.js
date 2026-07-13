// ARRL/RAC section abbreviations → full names, used for map hover tooltips.
// The shapes in public/map.svg carry ids matching these abbreviations.

// Sections that count toward the worked-sections tally.
export const TRACKED_SECTIONS = [
  // New England
  'CT', 'RI', 'EMA', 'WMA', 'ME', 'VT', 'NH',
  // New York / New Jersey
  'WNY', 'NNY', 'NNJ', 'ENY', 'SNJ', 'NLI',
  // Mid-Atlantic
  'DE', 'MDC', 'WPA', 'EPA',
  // Southeast
  'AL', 'VI', 'GA', 'KY', 'NFL', 'WCF', 'SFL', 'TN', 'NC', 'PR', 'SC', 'VA',
  // South Central
  'AR', 'LA', 'MS', 'NM', 'OK', 'NTX', 'WTX', 'STX',
  // Pacific / California
  'EB', 'LAX', 'ORG', 'SB', 'SCV', 'SDG', 'SF', 'SJV', 'SV', 'PAC',
  // Northwest / Mountain
  'AZ', 'EWA', 'WWA', 'ID', 'MT', 'NV', 'OR', 'UT', 'WY', 'AK',
  // Great Lakes
  'MI', 'OH', 'WV',
  // Central
  'IL', 'IN', 'WI',
  // Midwest
  'CO', 'IA', 'KS', 'MN', 'MO', 'NE', 'ND', 'SD',
  // Canada
  'AB', 'ONE', 'BC', 'ONN', 'GH', 'ONS', 'MB', 'PE', 'NB', 'QC', 'NL', 'SK', 'NS', 'TER',
  // DX and Mexico
  'DX', 'MX',
]

export const SECTION_NAMES = {
  // New England
  CT: 'Connecticut',
  RI: 'Rhode Island',
  EMA: 'Eastern Massachusetts',
  WMA: 'Western Massachusetts',
  ME: 'Maine',
  VT: 'Vermont',
  NH: 'New Hampshire',

  // New York / New Jersey
  WNY: 'Western New York',
  NNY: 'Northern New York',
  NNJ: 'Northern New Jersey',
  ENY: 'Eastern New York',
  SNJ: 'Southern New Jersey',
  NLI: 'New York City-Long Island',

  // Mid-Atlantic
  DE: 'Delaware',
  MDC: 'Maryland-DC',
  WPA: 'Western Pennsylvania',
  EPA: 'Eastern Pennsylvania',

  // Southeast
  AL: 'Alabama',
  VI: 'Virgin Islands',
  GA: 'Georgia',
  KY: 'Kentucky',
  NFL: 'Northern Florida',
  WCF: 'West Central Florida',
  SFL: 'Southern Florida',
  TN: 'Tennessee',
  NC: 'North Carolina',
  PR: 'Puerto Rico',
  SC: 'South Carolina',
  VA: 'Virginia',

  // South Central
  AR: 'Arkansas',
  LA: 'Louisiana',
  MS: 'Mississippi',
  NM: 'New Mexico',
  OK: 'Oklahoma',
  NTX: 'North Texas',
  WTX: 'West Texas',
  STX: 'South Texas',

  // Pacific / California
  EB: 'East Bay',
  LAX: 'Los Angeles',
  ORG: 'Orange County',
  SB: 'Santa Barbara',
  SCV: 'Santa Clara Valley',
  SDG: 'San Diego',
  SF: 'San Francisco',
  SJV: 'San Joaquin Valley',
  SV: 'Sacramento Valley',
  PAC: 'Pacific',

  // Northwest / Mountain
  AZ: 'Arizona',
  EWA: 'Eastern Washington',
  WWA: 'Western Washington',
  ID: 'Idaho',
  MT: 'Montana',
  NV: 'Nevada',
  OR: 'Oregon',
  UT: 'Utah',
  WY: 'Wyoming',
  AK: 'Alaska',

  // Great Lakes
  MI: 'Michigan',
  OH: 'Ohio',
  WV: 'West Virginia',

  // Central
  IL: 'Illinois',
  IN: 'Indiana',
  WI: 'Wisconsin',

  // Midwest
  CO: 'Colorado',
  IA: 'Iowa',
  KS: 'Kansas',
  MN: 'Minnesota',
  MO: 'Missouri',
  NE: 'Nebraska',
  ND: 'North Dakota',
  SD: 'South Dakota',

  // Canada
  AB: 'Alberta',
  ONE: 'Ontario East',
  BC: 'British Columbia',
  ONN: 'Ontario North',
  GH: 'Golden Horseshoe',
  ONS: 'Ontario South',
  MB: 'Manitoba',
  PE: 'Prince Edward Island',
  NB: 'New Brunswick',
  QC: 'Quebec',
  NL: 'Newfoundland/Labrador',
  SK: 'Saskatchewan',
  NS: 'Nova Scotia',
  TER: 'Territories',

  // DX and Mexico
  DX: 'DX (International)',
  MX: 'Mexico',
}
