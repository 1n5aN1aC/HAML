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

// Section abbreviations → full names, used for map hover tooltips and such.
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

// Section abbreviation -> 2-letter state/province code:
// Hand-maintained alongside STATE_TO_SECTION (its partial inverse).
export const SECTION_TO_STATE = {
  // New England:
  CT: 'CT', RI: 'RI', EMA: 'MA', WMA: 'MA', ME: 'ME', VT: 'VT', NH: 'NH',
  // New York / New Jersey
  WNY: 'NY', NNY: 'NY', ENY: 'NY', NLI: 'NY', NNJ: 'NJ', SNJ: 'NJ',
  // Mid-Atlantic
  DE: 'DE', MDC: 'MD', WPA: 'PA', EPA: 'PA',
  // Southeast
  AL: 'AL', GA: 'GA', KY: 'KY', NFL: 'FL', WCF: 'FL', SFL: 'FL', TN: 'TN', NC: 'NC', SC: 'SC', VA: 'VA',
  // South Central
  AR: 'AR', LA: 'LA', MS: 'MS', NM: 'NM', OK: 'OK', NTX: 'TX', WTX: 'TX', STX: 'TX',
  // Pacific / California
  EB: 'CA', LAX: 'CA', ORG: 'CA', SB: 'CA', SCV: 'CA', SDG: 'CA', SF: 'CA', SJV: 'CA', SV: 'CA', AZ: 'AZ',
  // Northwest / Mountain
  EWA: 'WA', WWA: 'WA', ID: 'ID', MT: 'MT', NV: 'NV', OR: 'OR', UT: 'UT', WY: 'WY', AK: 'AK',
  // Great Lakes
  MI: 'MI', OH: 'OH', WV: 'WV',
  // Central
  IL: 'IL', IN: 'IN', WI: 'WI',
  // Midwest
  CO: 'CO', IA: 'IA', KS: 'KS', MN: 'MN', MO: 'MO', NE: 'NE', ND: 'ND', SD: 'SD',
  // Canada
  AB: 'AB', BC: 'BC', ONE: 'ON', ONN: 'ON', GH: 'ON', ONS: 'ON', MB: 'MB', PE: 'PE', NB: 'NB', QC: 'QC', NL: 'NL', SK: 'SK', NS: 'NS',
  // DX and Mexico
  DX: 'DX', MX: 'DX', TER: 'DX', PAC: 'DX', VI: 'DX', PR: 'DX',
}

// State/province code -> section.
// Ambiguous entries are not included.  (States with multiple sections)
// Hand-maintained alongside SECTION_TO_STATE (its more-complete inverse).
export const STATE_TO_SECTION = {
  // New England
  CT: 'CT', RI: 'RI', ME: 'ME', VT: 'VT', NH: 'NH',
  // Mid-Atlantic
  DE: 'DE', MD: 'MDC',
  // Southeast
  AL: 'AL', GA: 'GA', KY: 'KY', TN: 'TN', NC: 'NC', SC: 'SC', VA: 'VA',
  // South Central
  AR: 'AR', LA: 'LA', MS: 'MS', NM: 'NM', OK: 'OK',
  // Northwest / Mountain
  AZ: 'AZ', ID: 'ID', MT: 'MT', NV: 'NV', OR: 'OR', UT: 'UT', WY: 'WY', AK: 'AK',
  // Great Lakes
  MI: 'MI', OH: 'OH', WV: 'WV',
  // Central
  IL: 'IL', IN: 'IN', WI: 'WI',
  // Midwest
  CO: 'CO', IA: 'IA', KS: 'KS', MN: 'MN', MO: 'MO', NE: 'NE', ND: 'ND', SD: 'SD',
  // Canada
  AB: 'AB', BC: 'BC', MB: 'MB', PE: 'PE', NB: 'NB', QC: 'QC', NL: 'NL', SK: 'SK', NS: 'NS',
  // DX and Mexico
  DX: 'DX',
}
