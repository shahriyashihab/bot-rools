"""
Unified country data module for consistent country code handling.
This module contains ALL world country codes with proper handling for:
- NANP countries (+1xxx)
- Russia/Kazakhstan (+7/+77)
- French overseas territories (+262, +590, etc.)
- All other overlapping codes

IMPORTANT: The get_sorted_country_codes() function sorts by length (longest first)
to ensure proper matching (e.g., +1829 matches before +1)
"""

COUNTRY_DATA = {
    # ============================================================
    # SOUTH ASIA
    # ============================================================
    "+880": {"name": "Bangladesh", "flag": "🇧🇩", "phone_length": 10},
    "+91": {"name": "India", "flag": "🇮🇳", "phone_length": 10},
    "+92": {"name": "Pakistan", "flag": "🇵🇰", "phone_length": 10},
    "+94": {"name": "Sri Lanka", "flag": "🇱🇰", "phone_length": 9},
    "+977": {"name": "Nepal", "flag": "🇳🇵", "phone_length": 10},
    "+975": {"name": "Bhutan", "flag": "🇧🇹", "phone_length": 8},
    "+960": {"name": "Maldives", "flag": "🇲🇻", "phone_length": 7},
    "+93": {"name": "Afghanistan", "flag": "🇦🇫", "phone_length": 9},
    
    # ============================================================
    # AFRICA
    # ============================================================
    "+213": {"name": "Algeria", "flag": "🇩🇿", "phone_length": 9},
    "+244": {"name": "Angola", "flag": "🇦🇴", "phone_length": 9},
    "+229": {"name": "Benin", "flag": "🇧🇯", "phone_length": 8},
    "+267": {"name": "Botswana", "flag": "🇧🇼", "phone_length": 8},
    "+226": {"name": "Burkina Faso", "flag": "🇧🇫", "phone_length": 8},
    "+257": {"name": "Burundi", "flag": "🇧🇮", "phone_length": 8},
    "+237": {"name": "Cameroon", "flag": "🇨🇲", "phone_length": 9},
    "+238": {"name": "Cape Verde", "flag": "🇨🇻", "phone_length": 7},
    "+236": {"name": "Central African Republic", "flag": "🇨🇫", "phone_length": 8},
    "+235": {"name": "Chad", "flag": "🇹🇩", "phone_length": 8},
    "+269": {"name": "Comoros", "flag": "🇰🇲", "phone_length": 7},
    "+242": {"name": "Congo", "flag": "🇨🇬", "phone_length": 9},
    "+243": {"name": "DR Congo", "flag": "🇨🇩", "phone_length": 9},
    "+225": {"name": "Côte d'Ivoire", "flag": "🇨🇮", "phone_length": 10},
    "+253": {"name": "Djibouti", "flag": "🇩🇯", "phone_length": 8},
    "+20": {"name": "Egypt", "flag": "🇪🇬", "phone_length": 10},
    "+240": {"name": "Equatorial Guinea", "flag": "🇬🇶", "phone_length": 9},
    "+291": {"name": "Eritrea", "flag": "🇪🇷", "phone_length": 7},
    "+251": {"name": "Ethiopia", "flag": "🇪🇹", "phone_length": 9},
    "+241": {"name": "Gabon", "flag": "🇬🇦", "phone_length": 8},
    "+220": {"name": "Gambia", "flag": "🇬🇲", "phone_length": 7},
    "+233": {"name": "Ghana", "flag": "🇬🇭", "phone_length": 9},
    "+224": {"name": "Guinea", "flag": "🇬🇳", "phone_length": 9},
    "+245": {"name": "Guinea-Bissau", "flag": "🇬🇼", "phone_length": 7},
    "+254": {"name": "Kenya", "flag": "🇰🇪", "phone_length": 9},
    "+266": {"name": "Lesotho", "flag": "🇱🇸", "phone_length": 8},
    "+231": {"name": "Liberia", "flag": "🇱🇷", "phone_length": 8},
    "+218": {"name": "Libya", "flag": "🇱🇾", "phone_length": 9},
    "+261": {"name": "Madagascar", "flag": "🇲🇬", "phone_length": 9},
    "+265": {"name": "Malawi", "flag": "🇲🇼", "phone_length": 9},
    "+223": {"name": "Mali", "flag": "🇲🇱", "phone_length": 8},
    "+222": {"name": "Mauritania", "flag": "🇲🇷", "phone_length": 8},
    "+230": {"name": "Mauritius", "flag": "🇲🇺", "phone_length": 8},
    "+212": {"name": "Morocco", "flag": "🇲🇦", "phone_length": 9},
    "+258": {"name": "Mozambique", "flag": "🇲🇿", "phone_length": 9},
    "+264": {"name": "Namibia", "flag": "🇳🇦", "phone_length": 9},
    "+227": {"name": "Niger", "flag": "🇳🇪", "phone_length": 8},
    "+234": {"name": "Nigeria", "flag": "🇳🇬", "phone_length": 10},
    "+250": {"name": "Rwanda", "flag": "🇷🇼", "phone_length": 9},
    "+239": {"name": "São Tomé and Príncipe", "flag": "🇸🇹", "phone_length": 7},
    "+221": {"name": "Senegal", "flag": "🇸🇳", "phone_length": 9},
    "+248": {"name": "Seychelles", "flag": "🇸🇨", "phone_length": 7},
    "+232": {"name": "Sierra Leone", "flag": "🇸🇱", "phone_length": 8},
    "+252": {"name": "Somalia", "flag": "🇸🇴", "phone_length": 9},
    "+27": {"name": "South Africa", "flag": "🇿🇦", "phone_length": 9},
    "+211": {"name": "South Sudan", "flag": "🇸🇸", "phone_length": 9},
    "+249": {"name": "Sudan", "flag": "🇸🇩", "phone_length": 9},
    "+268": {"name": "Eswatini", "flag": "🇸🇿", "phone_length": 8},
    "+255": {"name": "Tanzania", "flag": "🇹🇿", "phone_length": 9},
    "+228": {"name": "Togo", "flag": "🇹🇬", "phone_length": 8},
    "+216": {"name": "Tunisia", "flag": "🇹🇳", "phone_length": 8},
    "+256": {"name": "Uganda", "flag": "🇺🇬", "phone_length": 9},
    "+260": {"name": "Zambia", "flag": "🇿🇲", "phone_length": 9},
    "+263": {"name": "Zimbabwe", "flag": "🇿🇼", "phone_length": 9},
    
    # African territories
    # Réunion and Mayotte share +262, distinguished by next digits
    # Réunion: +262 262, +262 263, +262 692, +262 693
    # Mayotte: +262 269, +262 639
    "+262": {"name": "Réunion", "flag": "🇷🇪", "phone_length": 9},
    "+262262": {"name": "Réunion", "flag": "🇷🇪", "phone_length": 9},
    "+262263": {"name": "Réunion", "flag": "🇷🇪", "phone_length": 9},
    "+262692": {"name": "Réunion", "flag": "🇷🇪", "phone_length": 9},
    "+262693": {"name": "Réunion", "flag": "🇷🇪", "phone_length": 9},
    "+262269": {"name": "Mayotte", "flag": "🇾🇹", "phone_length": 9},
    "+262639": {"name": "Mayotte", "flag": "🇾🇹", "phone_length": 9},
    "+246": {"name": "British Indian Ocean Territory", "flag": "🇮🇴", "phone_length": 7},
    "+247": {"name": "Ascension Island", "flag": "🇦🇨", "phone_length": 5},
    "+290": {"name": "Saint Helena", "flag": "🇸🇭", "phone_length": 4},
    "+2908": {"name": "Tristan da Cunha", "flag": "🇹🇦", "phone_length": 4},
    
    # ============================================================
    # NORTH AMERICA - NANP COUNTRIES (+1)
    # Base code must be LAST so specific codes match first
    # ============================================================
    
    # USA Area Codes (examples - main territories)
    "+1": {"name": "USA/Canada", "flag": "🇺🇸", "phone_length": 10},
    
    # Caribbean NANP Countries (specific area codes)
    "+1242": {"name": "Bahamas", "flag": "🇧🇸", "phone_length": 10},
    "+1246": {"name": "Barbados", "flag": "🇧🇧", "phone_length": 10},
    "+1264": {"name": "Anguilla", "flag": "🇦🇮", "phone_length": 10},
    "+1268": {"name": "Antigua and Barbuda", "flag": "🇦🇬", "phone_length": 10},
    "+1284": {"name": "British Virgin Islands", "flag": "🇻🇬", "phone_length": 10},
    "+1340": {"name": "US Virgin Islands", "flag": "🇻🇮", "phone_length": 10},
    "+1345": {"name": "Cayman Islands", "flag": "🇰🇾", "phone_length": 10},
    "+1441": {"name": "Bermuda", "flag": "🇧🇲", "phone_length": 10},
    "+1473": {"name": "Grenada", "flag": "🇬🇩", "phone_length": 10},
    "+1649": {"name": "Turks and Caicos", "flag": "🇹🇨", "phone_length": 10},
    "+1664": {"name": "Montserrat", "flag": "🇲🇸", "phone_length": 10},
    "+1670": {"name": "Northern Mariana Islands", "flag": "🇲🇵", "phone_length": 10},
    "+1671": {"name": "Guam", "flag": "🇬🇺", "phone_length": 10},
    "+1684": {"name": "American Samoa", "flag": "🇦🇸", "phone_length": 10},
    "+1721": {"name": "Sint Maarten", "flag": "🇸🇽", "phone_length": 10},
    "+1758": {"name": "Saint Lucia", "flag": "🇱🇨", "phone_length": 10},
    "+1767": {"name": "Dominica", "flag": "🇩🇲", "phone_length": 10},
    "+1784": {"name": "Saint Vincent and the Grenadines", "flag": "🇻🇨", "phone_length": 10},
    "+1787": {"name": "Puerto Rico", "flag": "🇵🇷", "phone_length": 10},
    "+1809": {"name": "Dominican Republic", "flag": "🇩🇴", "phone_length": 10},
    "+1829": {"name": "Dominican Republic", "flag": "🇩🇴", "phone_length": 10},
    "+1849": {"name": "Dominican Republic", "flag": "🇩🇴", "phone_length": 10},
    "+1868": {"name": "Trinidad and Tobago", "flag": "🇹🇹", "phone_length": 10},
    "+1869": {"name": "Saint Kitts and Nevis", "flag": "🇰🇳", "phone_length": 10},
    "+1876": {"name": "Jamaica", "flag": "🇯🇲", "phone_length": 10},
    "+1658": {"name": "Jamaica", "flag": "🇯🇲", "phone_length": 10},
    "+1939": {"name": "Puerto Rico", "flag": "🇵🇷", "phone_length": 10},
    
    # ============================================================
    # CENTRAL & SOUTH AMERICA
    # ============================================================
    "+54": {"name": "Argentina", "flag": "🇦🇷", "phone_length": 10},
    "+591": {"name": "Bolivia", "flag": "🇧🇴", "phone_length": 8},
    "+55": {"name": "Brazil", "flag": "🇧🇷", "phone_length": 11},
    "+56": {"name": "Chile", "flag": "🇨🇱", "phone_length": 9},
    "+57": {"name": "Colombia", "flag": "🇨🇴", "phone_length": 10},
    "+506": {"name": "Costa Rica", "flag": "🇨🇷", "phone_length": 8},
    "+53": {"name": "Cuba", "flag": "🇨🇺", "phone_length": 8},
    "+502": {"name": "Guatemala", "flag": "🇬🇹", "phone_length": 8},
    "+504": {"name": "Honduras", "flag": "🇭🇳", "phone_length": 8},
    "+52": {"name": "Mexico", "flag": "🇲🇽", "phone_length": 10},
    "+505": {"name": "Nicaragua", "flag": "🇳🇮", "phone_length": 8},
    "+507": {"name": "Panama", "flag": "🇵🇦", "phone_length": 8},
    "+595": {"name": "Paraguay", "flag": "🇵🇾", "phone_length": 9},
    "+51": {"name": "Peru", "flag": "🇵🇪", "phone_length": 9},
    "+598": {"name": "Uruguay", "flag": "🇺🇾", "phone_length": 8},
    "+58": {"name": "Venezuela", "flag": "🇻🇪", "phone_length": 10},
    "+501": {"name": "Belize", "flag": "🇧🇿", "phone_length": 7},
    "+503": {"name": "El Salvador", "flag": "🇸🇻", "phone_length": 8},
    "+593": {"name": "Ecuador", "flag": "🇪🇨", "phone_length": 9},
    "+509": {"name": "Haiti", "flag": "🇭🇹", "phone_length": 8},
    "+592": {"name": "Guyana", "flag": "🇬🇾", "phone_length": 7},
    "+597": {"name": "Suriname", "flag": "🇸🇷", "phone_length": 7},
    "+500": {"name": "Falkland Islands", "flag": "🇫🇰", "phone_length": 5},
    "+508": {"name": "Saint Pierre and Miquelon", "flag": "🇵🇲", "phone_length": 6},
    
    # French overseas in Americas (+590, +594, +596)
    "+590": {"name": "Guadeloupe", "flag": "🇬🇵", "phone_length": 9},
    "+5901": {"name": "Guadeloupe", "flag": "🇬🇵", "phone_length": 9},
    "+5902": {"name": "Guadeloupe", "flag": "🇬🇵", "phone_length": 9},
    "+5903": {"name": "Guadeloupe", "flag": "🇬🇵", "phone_length": 9},
    "+5904": {"name": "Guadeloupe", "flag": "🇬🇵", "phone_length": 9},
    "+5905": {"name": "Guadeloupe", "flag": "🇬🇵", "phone_length": 9},
    "+5906": {"name": "Saint Martin", "flag": "🇲🇫", "phone_length": 9},
    "+5907": {"name": "Saint Barthélemy", "flag": "🇧🇱", "phone_length": 9},
    "+5908": {"name": "Saint Barthélemy", "flag": "🇧🇱", "phone_length": 9},
    "+5909": {"name": "Saint Martin", "flag": "🇲🇫", "phone_length": 9},
    "+594": {"name": "French Guiana", "flag": "🇬🇫", "phone_length": 9},
    "+596": {"name": "Martinique", "flag": "🇲🇶", "phone_length": 9},
    
    # Dutch Caribbean (+599)
    "+599": {"name": "Curaçao", "flag": "🇨🇼", "phone_length": 7},
    "+5993": {"name": "Caribbean Netherlands (Bonaire)", "flag": "🇧🇶", "phone_length": 7},
    "+5994": {"name": "Caribbean Netherlands (Saba)", "flag": "🇧🇶", "phone_length": 7},
    "+5995": {"name": "Caribbean Netherlands (Sint Eustatius)", "flag": "🇧🇶", "phone_length": 7},
    "+5997": {"name": "Curaçao", "flag": "🇨🇼", "phone_length": 7},
    "+5999": {"name": "Curaçao", "flag": "🇨🇼", "phone_length": 7},
    "+297": {"name": "Aruba", "flag": "🇦🇼", "phone_length": 7},
    
    # ============================================================
    # OCEANIA
    # ============================================================
    "+61": {"name": "Australia", "flag": "🇦🇺", "phone_length": 9},
    "+614": {"name": "Australia Mobile", "flag": "🇦🇺", "phone_length": 9},
    "+64": {"name": "New Zealand", "flag": "🇳🇿", "phone_length": 9},
    "+675": {"name": "Papua New Guinea", "flag": "🇵🇬", "phone_length": 8},
    "+676": {"name": "Tonga", "flag": "🇹🇴", "phone_length": 5},
    "+679": {"name": "Fiji", "flag": "🇫🇯", "phone_length": 7},
    "+680": {"name": "Palau", "flag": "🇵🇼", "phone_length": 7},
    "+682": {"name": "Cook Islands", "flag": "🇨🇰", "phone_length": 5},
    "+683": {"name": "Niue", "flag": "🇳🇺", "phone_length": 4},
    "+685": {"name": "Samoa", "flag": "🇼🇸", "phone_length": 7},
    "+686": {"name": "Kiribati", "flag": "🇰🇮", "phone_length": 5},
    "+687": {"name": "New Caledonia", "flag": "🇳🇨", "phone_length": 6},
    "+688": {"name": "Tuvalu", "flag": "🇹🇻", "phone_length": 5},
    "+689": {"name": "French Polynesia", "flag": "🇵🇫", "phone_length": 8},
    "+690": {"name": "Tokelau", "flag": "🇹🇰", "phone_length": 4},
    "+691": {"name": "Micronesia", "flag": "🇫🇲", "phone_length": 7},
    "+692": {"name": "Marshall Islands", "flag": "🇲🇭", "phone_length": 7},
    "+677": {"name": "Solomon Islands", "flag": "🇸🇧", "phone_length": 7},
    "+678": {"name": "Vanuatu", "flag": "🇻🇺", "phone_length": 7},
    "+681": {"name": "Wallis and Futuna", "flag": "🇼🇫", "phone_length": 6},
    "+674": {"name": "Nauru", "flag": "🇳🇷", "phone_length": 7},
    "+672": {"name": "Norfolk Island", "flag": "🇳🇫", "phone_length": 6},
    "+6721": {"name": "Australian Antarctic Territory", "flag": "🇦🇶", "phone_length": 6},
    "+6723": {"name": "Norfolk Island", "flag": "🇳🇫", "phone_length": 5},
    
    # ============================================================
    # EAST ASIA
    # ============================================================
    "+86": {"name": "China", "flag": "🇨🇳", "phone_length": 11},
    "+852": {"name": "Hong Kong", "flag": "🇭🇰", "phone_length": 8},
    "+853": {"name": "Macau", "flag": "🇲🇴", "phone_length": 8},
    "+886": {"name": "Taiwan", "flag": "🇹🇼", "phone_length": 9},
    "+81": {"name": "Japan", "flag": "🇯🇵", "phone_length": 10},
    "+82": {"name": "South Korea", "flag": "🇰🇷", "phone_length": 10},
    "+850": {"name": "North Korea", "flag": "🇰🇵", "phone_length": 10},
    "+976": {"name": "Mongolia", "flag": "🇲🇳", "phone_length": 8},
    
    # ============================================================
    # SOUTHEAST ASIA
    # ============================================================
    "+60": {"name": "Malaysia", "flag": "🇲🇾", "phone_length": 10},
    "+62": {"name": "Indonesia", "flag": "🇮🇩", "phone_length": 11},
    "+63": {"name": "Philippines", "flag": "🇵🇭", "phone_length": 10},
    "+65": {"name": "Singapore", "flag": "🇸🇬", "phone_length": 8},
    "+66": {"name": "Thailand", "flag": "🇹🇭", "phone_length": 9},
    "+84": {"name": "Vietnam", "flag": "🇻🇳", "phone_length": 10},
    "+855": {"name": "Cambodia", "flag": "🇰🇭", "phone_length": 9},
    "+856": {"name": "Laos", "flag": "🇱🇦", "phone_length": 10},
    "+95": {"name": "Myanmar", "flag": "🇲🇲", "phone_length": 10},
    "+673": {"name": "Brunei", "flag": "🇧🇳", "phone_length": 7},
    "+670": {"name": "Timor-Leste", "flag": "🇹🇱", "phone_length": 8},
    
    # ============================================================
    # MIDDLE EAST
    # ============================================================
    "+966": {"name": "Saudi Arabia", "flag": "🇸🇦", "phone_length": 9},
    "+971": {"name": "UAE", "flag": "🇦🇪", "phone_length": 9},
    "+970": {"name": "Palestine", "flag": "🇵🇸", "phone_length": 9},
    "+962": {"name": "Jordan", "flag": "🇯🇴", "phone_length": 9},
    "+965": {"name": "Kuwait", "flag": "🇰🇼", "phone_length": 8},
    "+974": {"name": "Qatar", "flag": "🇶🇦", "phone_length": 8},
    "+968": {"name": "Oman", "flag": "🇴🇲", "phone_length": 8},
    "+967": {"name": "Yemen", "flag": "🇾🇪", "phone_length": 9},
    "+961": {"name": "Lebanon", "flag": "🇱🇧", "phone_length": 8},
    "+963": {"name": "Syria", "flag": "🇸🇾", "phone_length": 9},
    "+964": {"name": "Iraq", "flag": "🇮🇶", "phone_length": 10},
    "+972": {"name": "Israel", "flag": "🇮🇱", "phone_length": 9},
    "+973": {"name": "Bahrain", "flag": "🇧🇭", "phone_length": 8},
    "+98": {"name": "Iran", "flag": "🇮🇷", "phone_length": 10},
    "+90": {"name": "Turkey", "flag": "🇹🇷", "phone_length": 10},
    
    # ============================================================
    # RUSSIA & CENTRAL ASIA (+7)
    # Kazakhstan uses +77, Russia uses +7 (other prefixes)
    # ============================================================
    "+7": {"name": "Russia", "flag": "🇷🇺", "phone_length": 10},
    "+76": {"name": "Kazakhstan", "flag": "🇰🇿", "phone_length": 10},
    "+77": {"name": "Kazakhstan", "flag": "🇰🇿", "phone_length": 10},
    "+992": {"name": "Tajikistan", "flag": "🇹🇯", "phone_length": 9},
    "+993": {"name": "Turkmenistan", "flag": "🇹🇲", "phone_length": 8},
    "+994": {"name": "Azerbaijan", "flag": "🇦🇿", "phone_length": 9},
    "+995": {"name": "Georgia", "flag": "🇬🇪", "phone_length": 9},
    "+996": {"name": "Kyrgyzstan", "flag": "🇰🇬", "phone_length": 9},
    "+998": {"name": "Uzbekistan", "flag": "🇺🇿", "phone_length": 9},
    
    # ============================================================
    # EUROPE - WESTERN
    # ============================================================
    "+33": {"name": "France", "flag": "🇫🇷", "phone_length": 9},
    "+44": {"name": "United Kingdom", "flag": "🇬🇧", "phone_length": 10},
    "+49": {"name": "Germany", "flag": "🇩🇪", "phone_length": 11},
    "+39": {"name": "Italy", "flag": "🇮🇹", "phone_length": 10},
    "+34": {"name": "Spain", "flag": "🇪🇸", "phone_length": 9},
    "+351": {"name": "Portugal", "flag": "🇵🇹", "phone_length": 9},
    "+31": {"name": "Netherlands", "flag": "🇳🇱", "phone_length": 9},
    "+32": {"name": "Belgium", "flag": "🇧🇪", "phone_length": 9},
    "+352": {"name": "Luxembourg", "flag": "🇱🇺", "phone_length": 9},
    "+41": {"name": "Switzerland", "flag": "🇨🇭", "phone_length": 9},
    "+43": {"name": "Austria", "flag": "🇦🇹", "phone_length": 10},
    "+353": {"name": "Ireland", "flag": "🇮🇪", "phone_length": 9},
    
    # UK Crown Dependencies & Territories
    "+441534": {"name": "Jersey", "flag": "🇯🇪", "phone_length": 10},
    "+441624": {"name": "Isle of Man", "flag": "🇮🇲", "phone_length": 10},
    "+441481": {"name": "Guernsey", "flag": "🇬🇬", "phone_length": 10},
    
    # ============================================================
    # EUROPE - NORTHERN
    # ============================================================
    "+45": {"name": "Denmark", "flag": "🇩🇰", "phone_length": 8},
    "+46": {"name": "Sweden", "flag": "🇸🇪", "phone_length": 9},
    "+47": {"name": "Norway", "flag": "🇳🇴", "phone_length": 8},
    "+358": {"name": "Finland", "flag": "🇫🇮", "phone_length": 10},
    "+354": {"name": "Iceland", "flag": "🇮🇸", "phone_length": 7},
    "+370": {"name": "Lithuania", "flag": "🇱🇹", "phone_length": 8},
    "+371": {"name": "Latvia", "flag": "🇱🇻", "phone_length": 8},
    "+372": {"name": "Estonia", "flag": "🇪🇪", "phone_length": 8},
    "+298": {"name": "Faroe Islands", "flag": "🇫🇴", "phone_length": 6},
    "+299": {"name": "Greenland", "flag": "🇬🇱", "phone_length": 6},
    "+3584": {"name": "Åland Islands", "flag": "🇦🇽", "phone_length": 10},
    
    # ============================================================
    # EUROPE - EASTERN
    # ============================================================
    "+48": {"name": "Poland", "flag": "🇵🇱", "phone_length": 9},
    "+420": {"name": "Czech Republic", "flag": "🇨🇿", "phone_length": 9},
    "+421": {"name": "Slovakia", "flag": "🇸🇰", "phone_length": 9},
    "+36": {"name": "Hungary", "flag": "🇭🇺", "phone_length": 9},
    "+40": {"name": "Romania", "flag": "🇷🇴", "phone_length": 9},
    "+359": {"name": "Bulgaria", "flag": "🇧🇬", "phone_length": 9},
    "+380": {"name": "Ukraine", "flag": "🇺🇦", "phone_length": 9},
    "+375": {"name": "Belarus", "flag": "🇧🇾", "phone_length": 9},
    "+373": {"name": "Moldova", "flag": "🇲🇩", "phone_length": 8},
    
    # ============================================================
    # EUROPE - SOUTHERN
    # ============================================================
    "+30": {"name": "Greece", "flag": "🇬🇷", "phone_length": 10},
    "+357": {"name": "Cyprus", "flag": "🇨🇾", "phone_length": 8},
    "+356": {"name": "Malta", "flag": "🇲🇹", "phone_length": 8},
    "+355": {"name": "Albania", "flag": "🇦🇱", "phone_length": 9},
    "+385": {"name": "Croatia", "flag": "🇭🇷", "phone_length": 9},
    "+386": {"name": "Slovenia", "flag": "🇸🇮", "phone_length": 8},
    "+387": {"name": "Bosnia and Herzegovina", "flag": "🇧🇦", "phone_length": 8},
    "+381": {"name": "Serbia", "flag": "🇷🇸", "phone_length": 9},
    "+382": {"name": "Montenegro", "flag": "🇲🇪", "phone_length": 8},
    "+383": {"name": "Kosovo", "flag": "🇽🇰", "phone_length": 8},
    "+389": {"name": "North Macedonia", "flag": "🇲🇰", "phone_length": 8},
    "+374": {"name": "Armenia", "flag": "🇦🇲", "phone_length": 8},
    
    # ============================================================
    # EUROPE - MICROSTATES
    # ============================================================
    "+376": {"name": "Andorra", "flag": "🇦🇩", "phone_length": 6},
    "+377": {"name": "Monaco", "flag": "🇲🇨", "phone_length": 8},
    "+378": {"name": "San Marino", "flag": "🇸🇲", "phone_length": 10},
    "+350": {"name": "Gibraltar", "flag": "🇬🇮", "phone_length": 8},
    "+423": {"name": "Liechtenstein", "flag": "🇱🇮", "phone_length": 7},
}


# ============================================================
# COUNTRY_GROUPS
# Sub-codes that belong to the SAME real territory. Used by the
# rate-fallback logic so that, e.g., a Jamaican +1658 number falls
# back to the +1876 (Jamaica) rate BEFORE the +1 (USA/Canada) rate.
# Derived from real ITU/TelephoneNumberingPlan data.
# ============================================================
COUNTRY_GROUPS = {
    "Jamaica": ["+1658", "+1876"],
    "Dominican Republic": ["+1809", "+1829", "+1849"],
    "Puerto Rico": ["+1787", "+1939"],
    "Réunion": ["+262262", "+262263", "+262692", "+262693"],
    "Mayotte": ["+262269", "+262639"],
    "Guadeloupe": ["+5901", "+5902", "+5903", "+5904", "+5905"],
    "Saint Martin": ["+5906", "+5909"],
    "Saint Barthélemy": ["+5907", "+5908"],
    "Caribbean Netherlands (Bonaire/Saba/St Eustatius)": ["+5993", "+5994", "+5995"],
    "Curaçao": ["+5997", "+5999"],
    "Kazakhstan": ["+76", "+77"],
    "Saint Helena & Tristan da Cunha": ["+290", "+2908"],
}

# Group lookup: sub-code -> full list of its territory's codes
_GROUP_BY_CODE = {}
for _codes in COUNTRY_GROUPS.values():
    for _code in _codes:
        _GROUP_BY_CODE[_code] = _codes


def get_territory_group_codes(country_code: str) -> list:
    """
    Return all COUNTRY_DATA codes that belong to the same real
    territory as the given code (including the code itself).
    Returns [country_code] when the code has no group.
    """
    if country_code in _GROUP_BY_CODE:
        return list(_GROUP_BY_CODE[country_code])
    return [country_code]


def get_sorted_country_codes():
    """
    Returns country codes sorted by length in descending order.
    This ensures longer codes (like +1876 for Jamaica) are matched before shorter ones (like +1 for USA).
    """
    return _SORTED_COUNTRY_CODES


_SORTED_COUNTRY_CODES = sorted(COUNTRY_DATA.keys(), key=len, reverse=True)


def get_country_code_from_phone(phone: str):
    """
    Extract the country code from a phone number.
    Uses length-sorted matching to ensure correct country detection.
    
    Args:
        phone: Phone number with country code (e.g., +18765551234)
    
    Returns:
        Country code (e.g., +1876) or None if not found
    """
    if not phone or not phone.startswith("+"):
        return None
    digits_only = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
    if not digits_only.startswith("+"):
        return None
    sorted_codes = _SORTED_COUNTRY_CODES
    for cc in sorted_codes:
        if digits_only.startswith(cc):
            return cc
    return None


def is_supported_country_code(country_code: str) -> bool:
    """
    Check whether a country code (e.g. "+880") exists in the real
    country-code database. Used by the admin panel to reject bogus codes.
    """
    if not country_code or not country_code.startswith("+"):
        return False
    return country_code in COUNTRY_DATA


def normalize_phone(phone: str) -> str:
    """
    Normalize a user-supplied phone number:
    strips spaces, dashes and parentheses, keeps leading '+'.
    Returns '' for obviously empty/invalid input.
    """
    if not phone:
        return ""
    phone = phone.strip()
    if phone.startswith("+"):
        head, _, tail = phone.partition("+")
        return "+" + "".join(ch for ch in tail if ch.isdigit())
    return "".join(ch for ch in phone if ch.isdigit())


def get_flag(country_code: str) -> str:
    """Get flag emoji for a country code"""
    data = COUNTRY_DATA.get(country_code)
    return data["flag"] if data else "🌍"


def get_country_alpha2(country_code: str) -> str:
    """Derive the ISO 3166-1 alpha-2 code from the country's flag emoji.

    Flag emojis are a pair of regional-indicator symbols that encode the
    alpha-2 code directly (e.g. U+1F1E6 U+1F1FF -> "AZ"). Returns "??" when
    the country has no known flag.
    """
    letters = []
    for ch in get_flag(country_code):
        cp = ord(ch)
        if 0x1F1E6 <= cp <= 0x1F1FF:
            letters.append(chr(ord("A") + (cp - 0x1F1E6)))
    return "".join(letters) if letters else "??"


def get_country_name(country_code: str) -> str:
    """Get country name for a country code"""
    data = COUNTRY_DATA.get(country_code)
    return data["name"] if data else country_code


def get_phone_length(country_code: str) -> int:
    """Get expected phone number length for a country code"""
    data = COUNTRY_DATA.get(country_code)
    return data["phone_length"] if data else 10


def get_country_info(country_code: str) -> tuple:
    """Get country name and flag as a tuple"""
    data = COUNTRY_DATA.get(country_code)
    if data:
        return (data["name"], data["flag"])
    return (country_code, "🌍")


def get_all_flags() -> dict:
    """Get a dictionary of all country codes to flag emojis"""
    return {code: data["flag"] for code, data in COUNTRY_DATA.items()}


def get_all_phone_lengths() -> dict:
    """Get a dictionary of all country codes to phone lengths"""
    return {code: data["phone_length"] for code, data in COUNTRY_DATA.items()}
