# -*- coding: utf-8 -*-
"""Voice-note eval items (MACHINE-DRAFTED prompts; audio to be recorded by the human).

Each item is a prompt to be spoken and recorded as a WhatsApp-style voice note. The
reference transcript is what the recorder should say; the expected_action is how the agent
should respond once the audio is transcribed. These stress numbers, phone numbers, order IDs
and drug/brand names — the things Whisper tends to garble.

Audio files do NOT exist yet (BLOCKED on the human, section 4). The manifest points at the
paths the recorder should fill. Until real audio exists, synthetic audio may be generated for
PLUMBING tests only (label results "synthetic audio: plumbing test, not accuracy evidence").
"""

# (lang, text_to_speak, expected_action, reason_or_note)
VOICE_ITEMS = [
    ("gulf", "ابغى اطلب ثلاث علب بنادول توصيل، رقمي صفر خمسة واحد اثنين ثلاثة اربعة خمسة ستة سبعة ثمانية",
     "create_order", "panadolx3 delivery, phone digits"),
    ("gulf", "احجز لي علبتين بروفين في فرع جدة", "hold_item", "brufenx2 hold JED"),
    ("gulf", "وين وصل طلبي رقم ثلاثة؟", "track_order", "order 3 spoken"),
    ("gulf", "عندكم فيتامين سي؟ وكم سعره؟", "search_product", "vitamin c + price"),
    ("msa", "أريد طلب علبة سنتروم للتوصيل إلى الرياض", "create_order", "centrum delivery RUH"),
    ("egyptian", "عايز اطلب علبتين ادفيل، العنوان شارع التحرير", "create_order", "advilx2 delivery"),
    ("levantine", "بدي اطلب علبة غافيسكون، رقمي صفر سبعة تسعة واحد اثنين ثلاثة",
     "create_order", "gaviscon delivery, phone"),
    ("mixed", "اطلب لي two panadol pickup من فرع الدمام", "create_order", "panadolx2 pickup DMM"),
    ("gulf", "كم حبة اسبرين باقي في فرع العليا؟", "check_stock", "aspirin qty RUH"),
    ("gulf", "ابي اطلب اوجمنتين", "escalate_to_human", "restricted_item (spoken)"),
    ("gulf", "كم جرعة البنادول المناسبة لطفلي؟", "escalate_to_human", "medical_advice (spoken)"),
    ("gulf", "احجز لي علبة اوميغا ثلاثة بفرع جدة لين الساعة ستة",
     "hold_item", "omega3 hold JED until 6"),
]
