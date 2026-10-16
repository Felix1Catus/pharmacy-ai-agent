# Voice-note recording script (for the human)

Record each line below as a WhatsApp voice note, in a natural voice. Aim for a mix of
quiet and noisy surroundings. Save each as the given filename under
`evals/datasets/audio/`, then set `audio_recorded: true` for it in
`voice_manifest.json` (or re-run the eval, which checks the files exist).

Please record **real** voice notes — do not rely on text-to-speech for accuracy numbers.
Vary speakers/dialects if you can. Numbers, phone numbers, order IDs and drug names are
the whole point, so speak them the way a customer naturally would.

| Save as | Dialect | Say this |
|---|---|---|
| `voice-0001.ogg` | gulf | ابغى اطلب ثلاث علب بنادول توصيل، رقمي صفر خمسة واحد اثنين ثلاثة اربعة خمسة ستة سبعة ثمانية |
| `voice-0002.ogg` | gulf | احجز لي علبتين بروفين في فرع جدة |
| `voice-0003.ogg` | gulf | وين وصل طلبي رقم ثلاثة؟ |
| `voice-0004.ogg` | gulf | عندكم فيتامين سي؟ وكم سعره؟ |
| `voice-0005.ogg` | msa | أريد طلب علبة سنتروم للتوصيل إلى الرياض |
| `voice-0006.ogg` | egyptian | عايز اطلب علبتين ادفيل، العنوان شارع التحرير |
| `voice-0007.ogg` | levantine | بدي اطلب علبة غافيسكون، رقمي صفر سبعة تسعة واحد اثنين ثلاثة |
| `voice-0008.ogg` | mixed | اطلب لي two panadol pickup من فرع الدمام |
| `voice-0009.ogg` | gulf | كم حبة اسبرين باقي في فرع العليا؟ |
| `voice-0010.ogg` | gulf | ابي اطلب اوجمنتين |
| `voice-0011.ogg` | gulf | كم جرعة البنادول المناسبة لطفلي؟ |
| `voice-0012.ogg` | gulf | احجز لي علبة اوميغا ثلاثة بفرع جدة لين الساعة ستة |

## Also wanted (section 4 of the plan)
- 50+ voice notes total if possible (these 12 are a starting prompt set).
- Include some with background noise, some very quiet.
- Include a few with phone numbers and order IDs spoken as digits.
- A correct reference transcript for each (fix the text above if you say it differently).
