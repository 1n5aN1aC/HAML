#!/usr/bin/env python3
"""
sections.py - the ARRL/RAC contest sections, and the geography they key on.

Shared by the three importers that assign a section, because a section is a
property of a PLACE, not of the licensee: importer_fcc.py tags an operator from
its county, importer_ca.py from its census division, and importer_boundaries.py
tags the county polygon itself so a point-in-polygon hit answers "what section
is this?" in the same row. All three must agree on the answer, so the tables
they agree on live here rather than in three copies that drift apart.

Nothing in this module does I/O, imports a third-party package, or knows about a
database. It is data plus two lookups over it, so an importer can import it
without taking on anything it would otherwise have avoided.

Names are matched EXACTLY as the boundary sources spell them - Census NAME
("St. Johns", "Miami-Dade"), StatCan CDNAME after the importers' bilingual
cleanup ("Greater Sudbury") - so an upstream rename surfaces as an unmapped name
in an importer's drift report instead of quietly moving a county into the wrong
section. Both importers that build tables report drift in both directions: a
county with no section, and a name here that no county has.
"""

# --- US -------------------------------------------------------------------- #

# The 48 contiguous states + DC. Each is one section named after its own USPS
# code unless it appears in SPLIT_SECTIONS below.
CONTIGUOUS_STATES = {
    "AL", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
    "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR",
    "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI",
    "WY",
}

# APO/FPO military mail: the station could be physically anywhere, so no
# section (nor DXCC entity) applies.
MILITARY_STATES = {"AA", "AE", "AP"}

# The 8 states split into several sections along county lines, as
# section -> '|'-separated Census county NAME values.
SPLIT_SECTIONS = {
    "CA": {
        "EB": "Alameda|Contra Costa|Napa|Solano",
        "LAX": "Los Angeles",
        "ORG": "Inyo|Orange|Riverside|San Bernardino",
        "SB": "San Luis Obispo|Santa Barbara|Ventura",
        "SCV": "Monterey|San Benito|San Mateo|Santa Clara|Santa Cruz",
        "SDG": "Imperial|San Diego",
        "SF": "Del Norte|Humboldt|Lake|Marin|Mendocino|San Francisco|Sonoma",
        "SJV":
            "Calaveras|Fresno|Kern|Kings|Madera|Mariposa|Merced|Mono|"
            "San Joaquin|Stanislaus|Tulare|Tuolumne",
        "SV":
            "Alpine|Amador|Butte|Colusa|El Dorado|Glenn|Lassen|Modoc|Nevada|"
            "Placer|Plumas|Sacramento|Shasta|Sierra|Siskiyou|Sutter|Tehama|"
            "Trinity|Yolo|Yuba",
    },
    "FL": {
        "NFL":
            "Alachua|Baker|Bay|Bradford|Calhoun|Citrus|Clay|Columbia|Dixie|"
            "Duval|Escambia|Flagler|Franklin|Gadsden|Gilchrist|Gulf|Hamilton|"
            "Hernando|Holmes|Jackson|Jefferson|Lafayette|Lake|Leon|Levy|"
            "Liberty|Madison|Marion|Nassau|Okaloosa|Orange|Putnam|Santa Rosa|"
            "Seminole|St. Johns|Sumter|Suwannee|Taylor|Union|Volusia|Wakulla|"
            "Walton|Washington",
        "SFL":
            "Brevard|Broward|Collier|Miami-Dade|Glades|Hendry|Indian River|"
            "Lee|Martin|Monroe|Okeechobee|Osceola|Palm Beach|St. Lucie",
        "WCF":
            "Charlotte|DeSoto|Hardee|Highlands|Hillsborough|Manatee|Pasco|"
            "Pinellas|Polk|Sarasota",
    },
    "MA": {
        "EMA":
            "Barnstable|Bristol|Dukes|Essex|Middlesex|Nantucket|Norfolk|"
            "Plymouth|Suffolk",
        "WMA": "Berkshire|Franklin|Hampden|Hampshire|Worcester",
    },
    "NJ": {
        "NNJ":
            "Bergen|Essex|Hudson|Hunterdon|Middlesex|Monmouth|Morris|Passaic|"
            "Somerset|Sussex|Union|Warren",
        "SNJ":
            "Atlantic|Burlington|Camden|Cape May|Cumberland|Gloucester|"
            "Mercer|Ocean|Salem",
    },
    "NY": {
        "ENY":
            "Albany|Columbia|Dutchess|Greene|Orange|Putnam|Rensselaer|"
            "Rockland|Saratoga|Schenectady|Sullivan|Ulster|Warren|Washington|"
            "Westchester",
        "NLI": "Bronx|Kings|Nassau|New York|Queens|Richmond|Suffolk",
        "NNY":
            "Clinton|Essex|Franklin|Fulton|Hamilton|Jefferson|Lewis|"
            "Montgomery|St. Lawrence|Schoharie",
        "WNY":
            "Allegany|Broome|Cattaraugus|Cayuga|Chautauqua|Chemung|Chenango|"
            "Cortland|Delaware|Erie|Genesee|Herkimer|Livingston|Madison|"
            "Monroe|Niagara|Oneida|Onondaga|Ontario|Orleans|Oswego|Otsego|"
            "Schuyler|Seneca|Steuben|Tioga|Tompkins|Wayne|Wyoming|Yates",
    },
    "PA": {
        "EPA":
            "Adams|Berks|Bradford|Bucks|Carbon|Chester|Columbia|Cumberland|"
            "Dauphin|Delaware|Juniata|Lackawanna|Lancaster|Lebanon|Lehigh|"
            "Luzerne|Lycoming|Monroe|Montgomery|Montour|Northampton|"
            "Northumberland|Perry|Philadelphia|Pike|Schuylkill|Snyder|"
            "Sullivan|Susquehanna|Tioga|Union|Wayne|Wyoming|York",
        "WPA":
            "Allegheny|Armstrong|Beaver|Bedford|Blair|Butler|Cambria|Cameron|"
            "Centre|Clarion|Clearfield|Clinton|Crawford|Elk|Erie|Fayette|"
            "Forest|Franklin|Fulton|Greene|Huntingdon|Indiana|Jefferson|"
            "Lawrence|McKean|Mercer|Mifflin|Potter|Somerset|Venango|Warren|"
            "Washington|Westmoreland",
    },
    "TX": {
        "NTX":
            "Anderson|Archer|Baylor|Bell|Bosque|Bowie|Brown|Camp|Cass|"
            "Cherokee|Clay|Collin|Comanche|Cooke|Coryell|Dallas|Delta|Denton|"
            "Eastland|Ellis|Erath|Falls|Fannin|Franklin|Freestone|Grayson|"
            "Gregg|Hamilton|Harrison|Henderson|Hill|Hood|Hopkins|Hunt|Jack|"
            "Johnson|Kaufman|Lamar|Lampasas|Limestone|McLennan|Marion|Mills|"
            "Montague|Morris|Nacogdoches|Navarro|Palo Pinto|Panola|Parker|"
            "Rains|Red River|Rockwall|Rusk|Shelby|Smith|Somervell|Stephens|"
            "Tarrant|Throckmorton|Titus|Upshur|Van Zandt|Wichita|Wilbarger|"
            "Wise|Wood|Young",
        "STX":
            "Angelina|Aransas|Atascosa|Austin|Bandera|Bastrop|Bee|Bexar|"
            "Blanco|Brazoria|Brazos|Brooks|Burleson|Burnet|Caldwell|Calhoun|"
            "Cameron|Chambers|Colorado|Comal|Concho|DeWitt|Dimmit|Duval|"
            "Edwards|Fayette|Fort Bend|Frio|Galveston|Gillespie|Goliad|"
            "Gonzales|Grimes|Guadalupe|Hardin|Harris|Hays|Hidalgo|Houston|"
            "Jackson|Jasper|Jefferson|Jim Hogg|Jim Wells|Karnes|Kendall|"
            "Kenedy|Kerr|Kimble|Kinney|Kleberg|La Salle|Lavaca|Lee|Leon|"
            "Liberty|Live Oak|Llano|Madison|Mason|Matagorda|Maverick|"
            "McCulloch|McMullen|Medina|Menard|Milam|Montgomery|Newton|Nueces|"
            "Orange|Polk|Real|Refugio|Robertson|Sabine|San Augustine|"
            "San Jacinto|San Patricio|San Saba|Starr|Travis|Trinity|Tyler|"
            "Uvalde|Val Verde|Victoria|Walker|Waller|Washington|Webb|Wharton|"
            "Willacy|Williamson|Wilson|Zapata|Zavala",
        "WTX":
            "Andrews|Armstrong|Bailey|Borden|Brewster|Briscoe|Callahan|"
            "Carson|Castro|Childress|Cochran|Coke|Coleman|Collingsworth|"
            "Cottle|Crane|Crockett|Crosby|Culberson|Dallam|Dawson|Deaf Smith|"
            "Dickens|Donley|Ector|El Paso|Fisher|Floyd|Foard|Gaines|Garza|"
            "Glasscock|Gray|Hale|Hall|Hansford|Hardeman|Hartley|Haskell|"
            "Hemphill|Hockley|Howard|Hudspeth|Hutchinson|Irion|Jeff Davis|"
            "Jones|Kent|King|Knox|Lamb|Lipscomb|Loving|Lubbock|Lynn|Martin|"
            "Midland|Mitchell|Moore|Motley|Nolan|Ochiltree|Oldham|Parmer|"
            "Pecos|Potter|Presidio|Randall|Reagan|Reeves|Roberts|Runnels|"
            "Schleicher|Scurry|Shackelford|Sherman|Sterling|Stonewall|Sutton|"
            "Swisher|Taylor|Terrell|Terry|Tom Green|Upton|Ward|Wheeler|"
            "Winkler|Yoakum",
    },
    "WA": {
        "EWA":
            "Adams|Asotin|Benton|Chelan|Columbia|Douglas|Ferry|Franklin|"
            "Garfield|Grant|Kittitas|Klickitat|Lincoln|Okanogan|Pend Oreille|"
            "Spokane|Stevens|Walla Walla|Whitman|Yakima",
        "WWA":
            "Clallam|Clark|Cowlitz|Grays Harbor|Island|Jefferson|King|Kitsap|"
            "Lewis|Mason|Pacific|Pierce|San Juan|Skagit|Skamania|Snohomish|"
            "Thurston|Wahkiakum|Whatcom",
    },
}

SPLIT_STATES = set(SPLIT_SECTIONS)

# state -> {county NAME: section}, as Phase 9 queries it.
SECTION_BY_COUNTY = {
    st: {county: sec for sec, counties in m.items()
         for county in counties.split("|")}
    for st, m in SPLIT_SECTIONS.items()
}

# Every other state/territory maps 1:1 to a section named after its own USPS
# code, except MD+DC (MDC) and HI plus the Pacific territories (PAC).
SECTION_BY_STATE = {
    s: s for s in (CONTIGUOUS_STATES - SPLIT_STATES - {"MD", "DC"})
                  | {"AK", "PR", "VI"}
}
SECTION_BY_STATE.update({"MD": "MDC", "DC": "MDC",
                         "HI": "PAC", "GU": "PAC", "AS": "PAC", "MP": "PAC"})


def us_section(state, county):
    """ARRL section for a US (state, county) pair; None if unmappable.

    Only the 8 split states consult `county`, so everywhere else a county
    rename cannot move an operator into the wrong section. `county` may be None
    (no coordinates, or the county phase was skipped), which leaves exactly the
    split states unmapped.
    """
    st = (state or "").strip().upper()
    if st in MILITARY_STATES:
        return None
    if st in SPLIT_STATES:
        return SECTION_BY_COUNTY[st].get(county)
    return SECTION_BY_STATE.get(st)


# --- Canada ---------------------------------------------------------------- #

# StatCan PRUID -> province code. PREABBR in the boundary file is "B.C." /
# "N.L.", not the two-letter code every table here keys on.
PRUID_TO_PROV = {
    "10": "NL", "11": "PE", "12": "NS", "13": "NB", "24": "QC", "35": "ON",
    "46": "MB", "47": "SK", "48": "AB", "59": "BC", "60": "YT", "61": "NT",
    "62": "NU",
}

# Province -> RAC section. Ontario is absent: it is split by CD below.
SECTION_BY_PROVINCE = {
    "NL": "NL", "NS": "NS", "NB": "NB", "PE": "PE", "QC": "QC", "MB": "MB",
    "SK": "SK", "AB": "AB", "BC": "BC",
    "YT": "TER", "NT": "TER", "NU": "TER",
}

# Ontario census division -> RAC section, per RAC's "Ontario Sections effective
# 01 Jan 2023". Nipissing is split by Algonquin Park, but its only populated
# area (North Bay) is in the ONN part.
ON_SECTION_BY_CD = {name: sec for sec, names in {
    "GH": "Durham|York|Toronto|Peel|Halton|Hamilton|Niagara",
    "ONE": "Stormont, Dundas and Glengarry|Prescott and Russell|Ottawa|"
           "Leeds and Grenville|Lanark|Frontenac|Lennox and Addington|Hastings|"
           "Prince Edward|Northumberland|Peterborough|Kawartha Lakes|"
           "Haliburton|Renfrew",
    "ONN": "Nipissing|Manitoulin|Sudbury|Greater Sudbury|Timiskaming|Cochrane|"
           "Algoma|Thunder Bay|Rainy River|Kenora",
    "ONS": "Dufferin|Wellington|Haldimand-Norfolk|Brant|Waterloo|Perth|Oxford|"
           "Elgin|Chatham-Kent|Essex|Lambton|Middlesex|Huron|Bruce|Grey|"
           "Simcoe|Muskoka|Parry Sound",
}.items() for name in names.split("|")}


def ca_section(province, cd_name):
    """RAC section for a Canadian (province, census division); None if unmappable.

    Only Ontario consults `cd_name` - every other province and the three
    territories are decided by the province code alone. `cd_name` may be None
    (no coordinates, or a whole-province placeholder coordinate), which leaves
    exactly Ontario unmapped.
    """
    pr = (province or "").strip().upper()
    if pr == "ON":
        return ON_SECTION_BY_CD.get(cd_name)
    return SECTION_BY_PROVINCE.get(pr)
