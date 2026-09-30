"""
intern_places.py
~~~~~~~~~~~~~~~~
Where places are: the tables the internship finder reads a location string with.

Only data. `intern_location` does the parsing and `intern_vocab` checks a stored
`st:XX` token against `US_STATES`; both import from here, and this imports
nothing, so the tables load under bare `python3` and can never be the reason a
module fails to import.

Every table is copied verbatim from the measured reference (spec 4.3.4). They
were tuned against a month of real postings — 17,548 rows — so an entry that
looks redundant is usually there because a board spelled a place that way. Add
to them; do not tidy them. A city moved between tables changes which postings a
user is shown, and nothing fails when that happens.

Lower case throughout except for codes: two-letter US state and ISO country
codes stay upper case, which is how the location strings write them.
"""

# Code -> lower-case name. DC and Puerto Rico are here because postings list them
# as places in the US, and `st:DC` is a filter a user can pick.
US_STATES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california",
    "CO": "colorado", "CT": "connecticut", "DE": "delaware", "FL": "florida", "GA": "georgia",
    "HI": "hawaii", "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa",
    "KS": "kansas", "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi",
    "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york",
    "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina",
    "SD": "south dakota", "TN": "tennessee", "TX": "texas", "UT": "utah", "VT": "vermont",
    "VA": "virginia", "WA": "washington", "WV": "west virginia", "WI": "wisconsin",
    "WY": "wyoming", "DC": "district of columbia", "PR": "puerto rico",
}
# Name -> code, plus the two ways boards write the capital.
STATE_BY_NAME = {v: k for k, v in US_STATES.items()}
STATE_BY_NAME["washington dc"] = "DC"
STATE_BY_NAME["washington d.c."] = "DC"
# Canadian provinces, by name here and by code below. `Waterloo, ON` reads as
# Canada because `ON` is a province code and not a state code.
CA_PROVINCES = {"british columbia": "BC", "ontario": "ON", "quebec": "QC", "alberta": "AB",
                "manitoba": "MB", "saskatchewan": "SK", "nova scotia": "NS",
                "new brunswick": "NB"}
CA_PROV_CODES = set(CA_PROVINCES.values())
# The three-letter prefix some boards put first (`GBR - Bristol, UK`) -> alpha-2.
ISO3 = {"USA": "US", "GBR": "GB", "AUS": "AU", "IND": "IN", "CAN": "CA", "POL": "PL",
        "DEU": "DE", "MEX": "MX", "CHN": "CN", "UKR": "UA", "FRA": "FR", "JPN": "JP",
        "BRA": "BR", "SGP": "SG", "ISR": "IL", "IRL": "IE", "NLD": "NL", "ESP": "ES",
        "ITA": "IT", "CHE": "CH", "SWE": "SE", "KOR": "KR", "TWN": "TW", "THA": "TH",
        "ARE": "AE", "SAU": "SA", "QAT": "QA", "BEL": "BE", "CZE": "CZ", "ROU": "RO",
        "HUN": "HU", "PHL": "PH", "MYS": "MY", "IDN": "ID", "VNM": "VN", "ZAF": "ZA",
        "NZL": "NZ", "ARG": "AR", "CHL": "CL", "COL": "CO", "PER": "PE", "TUR": "TR",
        "EGY": "EG", "AUT": "AT", "DNK": "DK", "NOR": "NO", "FIN": "FI", "PRT": "PT",
        "GRC": "GR", "KAZ": "KZ", "CRI": "CR", "PRI": "US"}
# Country names and their common short forms -> alpha-2.
COUNTRIES = {
    "united states": "US", "united states of america": "US", "usa": "US", "us": "US",
    "u.s.": "US", "u.s.a.": "US", "america": "US",
    "united kingdom": "GB", "uk": "GB", "england": "GB", "scotland": "GB", "wales": "GB",
    "ireland": "IE", "india": "IN", "germany": "DE", "france": "FR", "netherlands": "NL",
    "switzerland": "CH", "japan": "JP", "australia": "AU", "israel": "IL", "canada": "CA",
    "china": "CN", "poland": "PL", "spain": "ES", "italy": "IT", "sweden": "SE",
    "norway": "NO", "denmark": "DK", "serbia": "RS", "romania": "RO", "brazil": "BR",
    "mexico": "MX", "korea": "KR", "south korea": "KR", "taiwan": "TW", "singapore": "SG",
    "thailand": "TH", "vietnam": "VN", "philippines": "PH", "malaysia": "MY",
    "indonesia": "ID", "belgium": "BE", "austria": "AT", "portugal": "PT",
    "czech republic": "CZ", "czechia": "CZ", "hungary": "HU", "ukraine": "UA",
    "finland": "FI", "greece": "GR", "turkey": "TR", "egypt": "EG", "nigeria": "NG",
    "kenya": "KE", "south africa": "ZA", "argentina": "AR", "chile": "CL",
    "colombia": "CO", "peru": "PE", "costa rica": "CR", "uae": "AE",
    "united arab emirates": "AE", "saudi arabia": "SA", "qatar": "QA",
    "new zealand": "NZ", "hong kong": "HK", "luxembourg": "LU", "estonia": "EE",
    "lithuania": "LT", "latvia": "LV", "bulgaria": "BG", "croatia": "HR",
    "slovakia": "SK", "kazakhstan": "KZ", "pakistan": "PK", "bangladesh": "BD",
    "sri lanka": "LK", "morocco": "MA", "ghana": "GH", "chad": "TD", "iraq": "IQ",
    "kuwait": "KW", "bahrain": "BH", "oman": "OM", "jordan": "JO",
}
NON_US_CITIES = {
    # from internship_poller.NON_US plus places measured as misfiled 'unknown'
    "london": "GB", "dublin": "IE", "berlin": "DE", "paris": "FR", "amsterdam": "NL",
    "zurich": "CH", "geneva": "CH", "bangalore": "IN", "bengaluru": "IN", "hyderabad": "IN",
    "pune": "IN", "chennai": "IN", "gurgaon": "IN", "gurugram": "IN", "noida": "IN",
    "mumbai": "IN", "mohali": "IN", "tokyo": "JP", "osaka": "JP", "tsuyama": "JP",
    "singapore": "SG", "sydney": "AU", "melbourne": "AU", "port melbourne": "AU",
    "brisbane": "AU", "tel aviv": "IL", "haifa": "IL", "toronto": "CA", "vancouver": "CA",
    "montreal": "CA", "ottawa": "CA", "calgary": "CA", "waterloo": "CA", "munich": "DE",
    "hamburg": "DE", "stockholm": "SE", "oslo": "NO", "copenhagen": "DK", "helsinki": "FI",
    "warsaw": "PL", "krakow": "PL", "gdansk": "PL", "wroclaw": "PL", "prague": "CZ",
    "lisbon": "PT", "porto": "PT", "madrid": "ES", "barcelona": "ES", "milan": "IT",
    "rome": "IT", "belgrade": "RS", "bucharest": "RO", "sofia": "BG", "budapest": "HU",
    "vienna": "AT", "brussels": "BE", "bristol": "GB", "manchester": "GB",
    "edinburgh": "GB", "galway": "IE", "cork": "IE", "sao paulo": "BR",
    "são paulo": "BR", "piracicaba": "BR", "mexico city": "MX", "monterrey": "MX",
    "santa catarina": "MX", "bogota": "CO", "bogotá": "CO", "buenos aires": "AR",
    "santiago": "CL", "lima": "PE", "lagos": "NG", "nairobi": "KE", "cairo": "EG",
    "dubai": "AE", "abu dhabi": "AE", "riyadh": "SA", "seoul": "KR", "taipei": "TW",
    "hong kong": "HK", "shanghai": "CN", "beijing": "CN", "shenzhen": "CN",
    "hangzhou": "CN", "wuxi": "CN", "suzhou": "CN", "tianjin": "CN", "xuzhou": "CN",
    "qingdao": "CN", "kuala lumpur": "MY", "jakarta": "ID", "manila": "PH",
    "bangkok": "TH", "rayong": "TH", "ho chi minh": "VN", "ho chi minh city": "VN",
    "hanoi": "VN", "auckland": "NZ", "wellington": "NZ", "kyiv": "UA", "athens, gr": "GR",
    "one-north": "SG", "doha": "QA",
}
NON_US_REGIONS = {  # province/state names that pin a foreign country
    "jiangsu": "CN", "shandong": "CN", "guangdong": "CN", "zhejiang": "CN",
    "karnataka": "IN", "telangana": "IN", "maharashtra": "IN", "tamil nadu": "IN",
    "nuevo leon": "MX", "nuevo león": "MX", "sao paulo state": "BR", "new south wales": "AU",
    "victoria": "AU", "queensland": "AU", "bavaria": "DE", "lima": "PE",
    **{k: "CA" for k in CA_PROVINCES},
}
# Cities that are often written without a state, and the state they are in.
US_CITIES = {
    "san francisco": "CA", "new york": "NY", "new york city": "NY", "nyc": "NY",
    "seattle": "WA", "austin": "TX", "boston": "MA", "chicago": "IL",
    "los angeles": "CA", "palo alto": "CA", "mountain view": "CA", "sunnyvale": "CA",
    "bellevue": "WA", "denver": "CO", "atlanta": "GA", "minneapolis": "MN",
    "peoria": "IL", "san jose": "CA", "san diego": "CA", "irvine": "CA",
    "hawthorne": "CA", "redmond": "WA", "miami": "FL", "dallas": "TX",
    "houston": "TX", "pittsburgh": "PA", "philadelphia": "PA", "detroit": "MI",
    "phoenix": "AZ", "salt lake city": "UT", "raleigh": "NC", "baltimore": "MD",
    "brooklyn": "NY", "st. louis": "MO", "washington dc": "DC", "washington, dc": "DC",
    "washington d.c.": "DC", "menlo park": "CA", "foster city": "CA",
    "redwood city": "CA", "oakland": "CA", "berkeley": "CA", "santa clara": "CA",
    "el segundo": "CA", "long beach": "CA", "starbase": "TX", "mclean": "VA",
    "reston": "VA", "arlington": "VA", "huntsville": "AL", "cambridge, ma": "MA",
    "somerville": "MA", "chandler": "AZ", "tempe": "AZ", "scottsdale": "AZ",
    "nashville": "TN", "charlotte": "NC", "columbus": "OH", "cincinnati": "OH",
    "cleveland": "OH", "indianapolis": "IN", "kansas city": "MO", "omaha": "NE",
    "las vegas": "NV", "portland": "OR", "sacramento": "CA", "fremont": "CA",
    "hayward": "CA", "newark": "CA", "boulder": "CO", "golden": "CO",
    "mccarran": "NV", "carson city": "NV", "reno": "NV", "ann arbor": "MI",
    "madison": "WI", "milwaukee": "WI", "st. paul": "MN", "woonsocket": "RI",
    "hartford": "CT", "stamford": "CT", "jersey city": "NJ", "hoboken": "NJ",
    "princeton": "NJ", "tampa": "FL", "orlando": "FL", "jacksonville": "FL",
    "richmond, va": "VA", "irving": "TX", "plano": "TX", "fort worth": "TX",
    "san antonio": "TX", "louisville": "KY", "memphis": "TN", "new orleans": "LA",
    "honolulu": "HI", "anchorage": "AK", "vandenberg": "CA", "cape canaveral": "FL",
    "mcgregor": "TX", "bastrop": "TX", "long island": "NY", "the bronx": "NY",
    "queens": "NY", "manhattan": "NY",
}


# ---- Metro membership (spec 4.3.4). Lower-case city names. ----
METRO_CITIES = {
    "oc": frozenset({
        "irvine", "costa mesa", "santa ana", "anaheim", "newport beach", "huntington beach",
        "tustin", "lake forest", "foothill ranch", "mission viejo", "aliso viejo", "fullerton",
        "orange", "garden grove", "fountain valley", "brea", "seal beach", "laguna hills",
        "laguna niguel", "laguna beach", "laguna woods", "san clemente", "cypress", "buena park",
        "yorba linda", "placentia", "la habra", "rancho santa margarita", "san juan capistrano",
        "westminster", "los alamitos", "dana point", "ladera ranch", "stanton", "la palma"}),
    "la": frozenset({
        "los angeles", "el segundo", "hawthorne", "long beach", "torrance", "santa monica",
        "culver city", "burbank", "pasadena", "glendale", "carson", "manhattan beach",
        "redondo beach", "playa vista", "van nuys", "northridge", "woodland hills",
        "santa clarita", "valencia", "palmdale", "lancaster", "west hollywood", "beverly hills",
        "inglewood", "downey", "gardena", "el monte", "pomona", "city of industry", "commerce",
        "vernon", "sylmar", "chatsworth", "canoga park", "simi valley", "thousand oaks",
        "camarillo", "oxnard", "ventura", "monterey park", "la mirada", "cerritos", "norwalk",
        "whittier", "west covina", "alhambra", "arcadia", "monrovia", "irwindale", "compton",
        "lakewood", "lomita", "san pedro", "wilmington", "marina del rey", "venice", "calabasas",
        "agoura hills", "westlake village", "san fernando", "encino", "sherman oaks",
        "north hollywood", "studio city", "hollywood", "diamond bar", "walnut", "claremont",
        "la verne", "covina", "azusa", "glendora", "duarte", "south gate", "bellflower",
        "paramount", "signal hill", "rolling hills estates", "palos verdes estates"}),
    "sd": frozenset({
        "san diego", "carlsbad", "oceanside", "escondido", "chula vista", "la jolla",
        "del mar", "encinitas", "san marcos", "vista", "poway", "el cajon", "la mesa",
        "national city", "santee", "rancho bernardo", "sorrento valley", "coronado",
        "solana beach", "imperial beach", "lemon grove", "spring valley", "rancho santa fe"}),
    "ie": frozenset({
        "riverside", "san bernardino", "ontario", "rancho cucamonga", "fontana", "corona",
        "moreno valley", "temecula", "murrieta", "chino", "chino hills", "upland", "redlands",
        "rialto", "colton", "perris", "menifee", "hemet", "eastvale", "jurupa valley",
        "victorville", "hesperia", "apple valley", "palm springs", "palm desert", "indio",
        "banning", "beaumont", "loma linda", "highland", "yucaipa", "norco", "lake elsinore",
        "mira loma", "bloomington"}),
    "bay": frozenset({
        "san francisco", "oakland", "san jose", "palo alto", "mountain view", "sunnyvale",
        "santa clara", "menlo park", "redwood city", "foster city", "san mateo", "burlingame",
        "south san francisco", "fremont", "hayward", "newark", "berkeley", "emeryville",
        "cupertino", "milpitas", "los gatos", "campbell", "san carlos", "belmont", "brisbane",
        "san bruno", "millbrae", "daly city", "pleasanton", "livermore", "dublin", "san ramon",
        "walnut creek", "concord", "richmond", "alameda", "san leandro", "union city",
        "los altos", "saratoga", "morgan hill", "gilroy", "half moon bay", "sausalito",
        "san rafael", "novato", "petaluma", "santa rosa", "napa", "vallejo", "benicia",
        "pittsburg", "antioch", "danville", "moffett field"}),
    "sac": frozenset({
        "sacramento", "folsom", "roseville", "rancho cordova", "elk grove", "davis",
        "west sacramento", "citrus heights", "rocklin", "el dorado hills", "lincoln",
        "woodland", "mcclellan"}),
    "sea": frozenset({
        "seattle", "bellevue", "redmond", "kirkland", "everett", "tacoma", "renton", "bothell",
        "issaquah", "kent", "auburn", "lynnwood", "tukwila", "federal way", "woodinville",
        "sammamish", "mukilteo", "puyallup", "olympia", "bremerton", "des moines", "seatac",
        "burien", "shoreline", "mercer island", "frederickson"}),
    "pdx": frozenset({
        "portland", "beaverton", "hillsboro", "tigard", "lake oswego", "gresham", "vancouver",
        "tualatin", "wilsonville", "clackamas", "oregon city", "milwaukie"}),
    "phx": frozenset({
        "phoenix", "tempe", "chandler", "scottsdale", "mesa", "gilbert", "glendale", "peoria",
        "goodyear", "avondale", "surprise", "queen creek", "buckeye", "tolleson"}),
    "den": frozenset({
        "denver", "boulder", "aurora", "lakewood", "littleton", "englewood", "golden",
        "centennial", "broomfield", "louisville", "westminster", "thornton", "arvada",
        "longmont", "greenwood village", "lone tree", "castle rock", "parker", "commerce city"}),
    "chi": frozenset({
        "chicago", "evanston", "naperville", "schaumburg", "oak brook", "skokie", "elk grove village",
        "rosemont", "itasca", "lombard", "downers grove", "aurora", "joliet", "deerfield",
        "northbrook", "lake forest", "arlington heights", "des plaines", "oak park", "wheaton",
        "hoffman estates", "bolingbrook", "melrose park", "norridge", "glenview", "lisle",
        "romeoville", "elgin", "waukegan", "libertyville", "vernon hills", "mount prospect",
        "algonquin"}),
    "dc": frozenset({
        "washington", "arlington", "alexandria", "mclean", "reston", "herndon", "tysons",
        "falls church", "fairfax", "chantilly", "vienna", "bethesda", "rockville",
        "silver spring", "gaithersburg", "college park", "greenbelt", "annapolis junction",
        "columbia", "laurel", "springfield", "sterling", "ashburn", "dulles", "manassas",
        "crystal city", "fort meade", "hanover", "linthicum", "national harbor", "leesburg"}),
    "nyc": frozenset({
        "new york", "new york city", "nyc", "manhattan", "brooklyn", "queens", "the bronx",
        "bronx", "staten island", "jersey city", "hoboken", "newark", "long island city",
        "white plains", "stamford", "greenwich", "yonkers", "weehawken", "harrison",
        "secaucus", "paramus", "edison", "princeton", "morristown", "purchase", "armonk",
        "rye brook", "tarrytown", "garden city", "melville", "uniondale", "hauppauge",
        "long island"}),
    "bos": frozenset({
        "boston", "cambridge", "somerville", "waltham", "burlington", "lexington", "woburn",
        "quincy", "needham", "newton", "watertown", "bedford", "marlborough", "framingham",
        "andover", "wilmington", "lowell", "billerica", "chelmsford", "braintree",
        "medford", "malden", "brookline", "natick", "wellesley", "canton", "westborough",
        "boxborough", "maynard", "danvers", "beverly", "salem", "tewksbury", "sturbridge"}),
    "atl": frozenset({
        "atlanta", "alpharetta", "marietta", "sandy springs", "duluth", "norcross",
        "kennesaw", "smyrna", "roswell", "lawrenceville", "peachtree corners", "decatur",
        "johns creek", "suwanee", "dunwoody", "buford", "fayetteville", "peachtree city",
        "college park", "east point", "stone mountain", "cumming", "mcdonough", "douglasville"}),
}
# The state each metro lives in (a city name is only a metro member in this state).
METRO_STATE = {"oc": "CA", "la": "CA", "sd": "CA", "ie": "CA", "bay": "CA", "sac": "CA",
               "sea": "WA", "pdx": "OR", "phx": "AZ", "den": "CO", "chi": "IL", "dc": "DC",
               "nyc": "NY", "bos": "MA", "atl": "GA"}
# Metros that span states: a city in any of these states counts.
METRO_EXTRA_STATES = {"pdx": {"WA"}, "dc": {"VA", "MD"}, "nyc": {"NJ", "CT"}}
# Southern California = these metros plus these extra cities (all CA).
SOCAL_METROS = frozenset({"oc", "la", "sd", "ie"})
SOCAL_EXTRA_CITIES = frozenset({"santa barbara", "goleta", "santa maria", "san luis obispo",
                                "bakersfield", "lompoc", "vandenberg", "vandenberg sfb",
                                "el centro", "mojave", "edwards", "china lake", "ridgecrest"})
# Region phrases found in location strings or titles -> (metro id or "", state).
METRO_PHRASES = (
    (r"\b(greater los angeles|los angeles county|los angeles area|south bay|san fernando valley|east la|west la)\b", "la", "CA"),
    (r"\b(orange county)\b", "oc", "CA"),
    (r"\b(inland empire)\b", "ie", "CA"),
    (r"\b(san diego county|greater san diego)\b", "sd", "CA"),
    (r"\b(southern california|socal)\b", "", "CA"),
    (r"\b(bay area|east bay|north bay|silicon valley|peninsula)\b", "bay", "CA"),
    (r"\b(northern california|norcal|central valley)\b", "", "CA"),
    (r"\b(greater sacramento)\b", "sac", "CA"),
    (r"\b(puget sound|greater seattle)\b", "sea", "WA"),
    (r"\b(metro atlanta|greater atlanta)\b", "atl", "GA"),
    (r"\b(chicagoland|greater chicago)\b", "chi", "IL"),
    (r"\b(twin cities|greater minneapolis)\b", "", "MN"),
    (r"\b(south florida|florida panhandle)\b", "", "FL"),
    (r"\b(long island|new york city|nyc|greater new york)\b", "nyc", "NY"),
    (r"\b(greater boston)\b", "bos", "MA"),
    (r"\b(dfw|dallas[- ]fort worth)\b", "", "TX"),
    (r"\b(greater phoenix)\b", "phx", "AZ"),
    (r"\b(greater denver|front range)\b", "den", "CO"),
    (r"\b(dmv|greater washington)\b", "dc", "DC"),
)
