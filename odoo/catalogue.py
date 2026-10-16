"""Dummy pharmacy catalogue (machine-drafted, NOT native-reviewed).

Arabic names/ingredients are machine-drafted and must be reviewed by a native speaker
before being treated as accuracy evidence (see docs/progress.md BLOCKED).

Fields per product:
  code         internal reference (stable key for idempotent seeding)
  name_en      English display name
  name_ar      Arabic display name
  brand        brand name (may equal name_en)
  ingredient_en / ingredient_ar  active ingredient
  price        list price (SAR)
  restricted   True => agent must NEVER order/hold; escalate
  rx           requires prescription (also treated as restricted by guardrails)
"""

BRANCHES = [
    {"name": "Riyadh - Olaya", "code": "RUH"},
    {"name": "Jeddah - Tahlia", "code": "JED"},
    {"name": "Dammam - Corniche", "code": "DMM"},
]

# (code, name_en, name_ar, brand, ingredient_en, ingredient_ar, price, restricted, rx)
_P = [
    ("PH-0001", "Panadol 500mg", "بنادول ٥٠٠ ملغ", "Panadol", "Paracetamol", "باراسيتامول", 12.0, False, False),
    ("PH-0002", "Panadol Extra", "بنادول اكسترا", "Panadol", "Paracetamol+Caffeine", "باراسيتامول وكافيين", 15.0, False, False),
    ("PH-0003", "Panadol Cold & Flu", "بنادول كولد اند فلو", "Panadol", "Paracetamol+Phenylephrine", "باراسيتامول وفينيلافرين", 18.0, False, False),
    ("PH-0004", "Adol 500mg", "ادول ٥٠٠", "Adol", "Paracetamol", "باراسيتامول", 10.0, False, False),
    ("PH-0005", "Fevadol", "فيفادول", "Fevadol", "Paracetamol", "باراسيتامول", 9.5, False, False),
    ("PH-0006", "Cetal", "سيتال", "Cetal", "Paracetamol", "باراسيتامول", 8.0, False, False),
    ("PH-0007", "Brufen 400mg", "بروفين ٤٠٠", "Brufen", "Ibuprofen", "ايبوبروفين", 16.0, False, False),
    ("PH-0008", "Advil", "ادفيل", "Advil", "Ibuprofen", "ايبوبروفين", 17.0, False, False),
    ("PH-0009", "Aspirin 100mg", "اسبرين ١٠٠", "Aspirin", "Acetylsalicylic acid", "حمض اسيتيل ساليسيليك", 7.0, False, False),
    ("PH-0010", "Congestal", "كونجستال", "Congestal", "Paracetamol+Chlorpheniramine", "باراسيتامول وكلورفينيرامين", 11.0, False, False),
    ("PH-0011", "Strepsils Lozenges", "ستربسلز", "Strepsils", "Amylmetacresol", "اميل ميتاكريزول", 14.0, False, False),
    ("PH-0012", "Otrivin Nasal Spray", "اوتريفين بخاخ الانف", "Otrivin", "Xylometazoline", "زايلوميتازولين", 19.0, False, False),
    ("PH-0013", "Gaviscon", "جافيسكون", "Gaviscon", "Sodium alginate", "الجينات الصوديوم", 22.0, False, False),
    ("PH-0014", "Rennie Antacid", "ريني", "Rennie", "Calcium carbonate", "كربونات الكالسيوم", 13.0, False, False),
    ("PH-0015", "Betadine Antiseptic", "بيتادين", "Betadine", "Povidone iodine", "بوفيدون يود", 20.0, False, False),
    ("PH-0016", "Dettol Antiseptic", "ديتول", "Dettol", "Chloroxylenol", "كلوروكسيلينول", 18.0, False, False),
    ("PH-0017", "Vitamin C 1000mg", "فيتامين سي ١٠٠٠", "Redoxon", "Ascorbic acid", "حمض الاسكوربيك", 25.0, False, False),
    ("PH-0018", "Vitamin D3 5000IU", "فيتامين د ٣", "Devit", "Cholecalciferol", "كوليكالسيفيرول", 30.0, False, False),
    ("PH-0019", "Centrum Multivitamin", "سنتروم", "Centrum", "Multivitamin", "فيتامينات متعددة", 60.0, False, False),
    ("PH-0020", "Omega-3 Fish Oil", "اوميغا ٣", "Omega", "Fish oil", "زيت السمك", 45.0, False, False),
    ("PH-0021", "Calcium + D", "كالسيوم مع فيتامين د", "Caltrate", "Calcium+VitD", "كالسيوم وفيتامين د", 35.0, False, False),
    ("PH-0022", "Iron Supplement", "مكمل الحديد", "Ferose", "Ferrous sulfate", "كبريتات الحديد", 28.0, False, False),
    ("PH-0023", "Zinc 50mg", "زنك ٥٠", "Zinca", "Zinc", "زنك", 22.0, False, False),
    ("PH-0024", "ORS Rehydration Salts", "محلول معالجة الجفاف", "Rehydran", "Oral rehydration salts", "املاح الارواء الفموي", 6.0, False, False),
    ("PH-0025", "CeraVe Moisturizer", "سيرافي مرطب", "CeraVe", "Ceramides", "سيراميد", 70.0, False, False),
    ("PH-0026", "Sunscreen SPF50", "واقي شمس", "LaRochePosay", "Sunscreen", "واقي شمس", 95.0, False, False),
    ("PH-0027", "Pampers Diapers M", "حفاضات بامبرز وسط", "Pampers", "Diapers", "حفاضات", 55.0, False, False),
    ("PH-0028", "Nan Baby Formula 1", "حليب نان رقم ١", "Nan", "Infant formula", "حليب اطفال", 65.0, False, False),
    ("PH-0029", "Signal Toothpaste", "معجون سيجنال", "Signal", "Fluoride toothpaste", "معجون اسنان بالفلورايد", 12.0, False, False),
    ("PH-0030", "Hand Sanitizer 500ml", "معقم اليدين", "Dettol", "Ethanol gel", "جل كحولي", 16.0, False, False),
    ("PH-0031", "Surgical Face Masks 50", "كمامات طبية", "MedMask", "Face masks", "كمامات", 20.0, False, False),
    ("PH-0032", "Digital Thermometer", "ميزان حرارة رقمي", "Hartmann", "Thermometer", "ميزان حرارة", 40.0, False, False),
    ("PH-0033", "Pregnancy Test", "اختبار حمل", "Clearblue", "hCG test", "اختبار الحمل", 30.0, False, False),
    ("PH-0034", "Clearblue Ovulation Test", "اختبار التبويض", "Clearblue", "LH test", "اختبار التبويض", 50.0, False, False),
    ("PH-0035", "Band-Aid Plasters", "بلاستر جروح", "Band-Aid", "Adhesive bandage", "لاصق جروح", 9.0, False, False),
    # Restricted / prescription — agent must ESCALATE, never order/hold:
    ("RX-0001", "Augmentin 1g", "اوجمنتين ١ جرام", "Augmentin", "Amoxicillin+Clavulanate", "اموكسيسيلين وكلافولانيك", 45.0, True, True),
    ("RX-0002", "Amoxil 500mg", "اموكسيل ٥٠٠", "Amoxil", "Amoxicillin", "اموكسيسيلين", 30.0, True, True),
    ("RX-0003", "Ventolin Inhaler", "فينتولين بخاخ", "Ventolin", "Salbutamol", "سالبوتامول", 25.0, True, True),
    ("RX-0004", "Lantus Insulin", "انسولين لانتوس", "Lantus", "Insulin glargine", "انسولين جلارجين", 120.0, True, True),
    ("RX-0005", "Tramadol 50mg", "ترامادول ٥٠", "Tramal", "Tramadol", "ترامادول", 0.0, True, True),
    ("RX-0006", "Xanax 0.5mg", "زاناكس ٠.٥", "Xanax", "Alprazolam", "البرازولام", 0.0, True, True),
    ("RX-0007", "Concerta 18mg", "كونسيرتا ١٨", "Concerta", "Methylphenidate", "ميثيل فينيديت", 0.0, True, True),
]

PRODUCTS = [
    dict(code=c, name_en=ne, name_ar=na, brand=b, ingredient_en=ie, ingredient_ar=ia,
         price=p, restricted=r, rx=rx)
    for (c, ne, na, b, ie, ia, p, r, rx) in _P
]
