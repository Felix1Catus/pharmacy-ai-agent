# Regulatory compliance review (Saudi Arabia) - assistant + Odoo

Reviewed 2026-11-06. **Not legal advice.** This maps what the public sources say to what the
assistant does, and lists what changed in the code. Most sources found are law-firm, academic or
vendor summaries, not the official texts; every "verify" item needs the pharmacy's licensed
pharmacist-in-charge and a Saudi lawyer before real customers use the service.

## 1. What the rules say (sources at the end)

| Area | Rule (as found) | Strength of source |
|---|---|---|
| Online pharmacy | Online ordering must run from a **licensed physical pharmacy**; a **Saudi pharmacist must be available**; the **commercial registration, SFDA licence and MOH licence numbers** must be shown on the website/app [1] | academic study (2024) |
| Who may advise / dispense | Only a **licensed pharmacist** dispenses; no prescription-only medicine without a **licensed physician's prescription** (OTC excepted); **generic substitution** by the pharmacist **with the patient's consent**; a pharmacy technician only under a pharmacist's supervision [6][11] | Health Professions Practice Law text (MOH PDF) + summaries |
| Prescription-only online | One legal handbook says POMs "cannot be offered through distance selling or via post" [2]; chain pharmacies in practice take a prescription upload before delivery [9] | conflicting - **verify** |
| Advertising | Prescription medicines **may not be advertised to the public**; OTC advertising needs **SFDA approval** (approval number shown) [3][4] | consistent across sources |
| Controlled / scheduled | Narcotic and psychotropic medicines: prescription-gated, SFDA-controlled (CDS permits); penalties under the narcotics law [9] | secondary - verify schedules (updated 2026-09) |
| Prices | The **SFDA price is a ceiling** [8] | pricing guideline |
| Products sold | Only **SFDA-registered** products may be sold, including online [7] | secondary |
| Seller identity (e-commerce law, Art. 6) | Name, address, contact, **commercial registration**, tax number on the store; register the store with the Ministry of Commerce [10] | law-firm summaries |
| Health data (PDPL) | Health data is **sensitive**: **explicit consent per purpose**, records of consent, withdrawal at any time, never used for marketing, legitimate interest not a basis, minimum necessary, **72-hour breach notice to SDAIA**, **cross-border transfer only to adequate countries / with safeguards**, registration on the National Data Governance Platform and a DPO where sensitive data is core [5] | SDAIA guide + implementing regulation |
| AI | SDAIA AI Ethics Principles / GenAI guidelines: **disclose that users talk to AI** (not binding) [12]; SFDA MDS-G010: software that diagnoses, manages or predicts disease from medical data is a **medical device** needing marketing authorization [13] | official guidance |

## 2. The assistant against those rules

| # | Behaviour | Before | Now |
|---|---|---|---|
| 1 | "Something for a sore throat / cough" | listed OTC products for the symptom (the chat chose medicines - a pharmacist's act, and close to clinical decision support) | **no product list**: fixed reply - a licensed pharmacist at any branch advises, or name the product. The model is no longer offered symptom search; the old pharmacist-approved table only returns with `SYMPTOM_SUGGESTIONS=1` |
| 2 | Dose / medical questions | refused + handover | refused unless about an order/cart item; then the **prescription's own words** are read back (OCR), marked "read automatically - check the pharmacy label", pharmacist only on dispute |
| 3 | Scheduled (مقيد) | refused + handover | refused, in person with a stamped prescription only, availability by branch, never priced/ordered/escalated |
| 4 | Prescription-only | ordered as a quotation; pharmacist reviews the photo before dispensing | unchanged - **verify** against [2] (see open items) |
| 5 | Prices | SFDA price | checked: 0 of 2,013 SFDA-priced products above the SFDA price |
| 6 | Health data consent | prescriptions stored without consent | **explicit consent first** (nothing read or stored before ✅), recorded on the contact (`pdpl_health_consent`, notice version), **"withdraw consent / delete my data"** records the withdrawal and queues a privacy request for staff |
| 7 | AI disclosure | none | greeting says it is an automated assistant and a licensed pharmacist advises |
| 8 | Seller identity | none | menu footer shows `STORE_NAME`, `STORE_CR`, `STORE_MOH_LICENSE`, `STORE_SFDA_LICENSE`, `STORE_VAT`, `STORE_ADDRESS`, `STORE_PHONE`, `PRIVACY_URL` from `.env` - **empty until the pharmacy fills them** |
| 9 | Chat logs | `results/audit.jsonl` kept forever (all messages, health questions included) | `scripts/prune_logs.py` deletes entries older than `LOG_RETENTION_DAYS` (default 90) from the audit log, handover log, WhatsApp outbox, saved chats and voice files; scheduled daily by `pharmacy-prune-logs.timer`. Prescription contents are no longer written to the audit log, a waiting prescription photo is never written to a session file, and all of these files are private to the service user (0600) |
| 12 | Who can read prescriptions | every internal Odoo user | only the group *Pharmacy: pharmacist* (and the administrator); the assistant runs as its own service account, which stores prescriptions but cannot approve them or reach Odoo's settings |
| 13 | Dispensing rules | enforced only in the assistant's code | Odoo itself refuses to confirm a quotation whose prescription is awaiting / pending / rejected, and a chat order containing a scheduled medicine |
| 14 | Customer identity | WhatsApp number, phone **or the unverified alternative phone** | the sender's own WhatsApp number (or the main phone on file); a typed number never identifies a WhatsApp chat |
| 10 | Invented facts | the model once invented opening hours and a Tabuk branch | branch / hours questions are answered from the branch records; numbers in such answers must come from a tool |
| 11 | Language | English questions after an Arabic start got Arabic replies | replies follow the customer's language |

## 3. Open items - not solvable in code

1. **Licences**: the service must run under the pharmacy's MOH licence with a Saudi pharmacist available during service hours; fill the `STORE_*` values; register the store with the Ministry of Commerce (Business Platform / Maroof).
2. **Hosting location**: this server is in the **United States** (Vultr, New Jersey). Real customers' health data processed here is a cross-border transfer under the PDPL; host production **inside the Kingdom** (and check the NCA / MOH data-hosting controls for health data).
3. **PDPL organisation**: privacy policy (link in `PRIVACY_URL`), registration on the National Data Governance Platform, DPO, breach-response process (72 h to SDAIA), a retention schedule for Odoo records, a process for access / deletion requests (they arrive as `privacy_request` handovers).
4. **Prescription-only medicines online**: confirm with counsel/MOH that quotation + prescription photo + pharmacist review + delivery is permitted for this licence ([2] says no distance selling; market practice [9] says upload-then-deliver). Consider Wasfaty integration instead of photos.
5. **Dose read-back (OCR)**: repeating the doctor's directions is transcription, not advice, but an OCR error could harm; confirm with the pharmacist-in-charge, and with counsel whether it falls under SFDA MDS-G010 / MDS-G27 (software medical device).
6. **Prescription-only alternatives**: the chat lists other brands with the same active ingredient; substitution is the pharmacist's decision with the patient's consent - the pharmacist still reviews before dispensing, but confirm this listing is acceptable.
7. **Controlled-medicine availability**: showing which branch stocks a scheduled medicine was the owner's decision; confirm with the pharmacist-in-charge.
8. **Training data**: the distillation set is synthetic (Claude wrote the customer messages), so no customer data went abroad. Never send real customer chats to an external model without a legal basis and transfer safeguards.

## Sources
1. [Overview of online pharmacy regulations in Saudi Arabia and the GCC (Frontiers in Pharmacology, 2024)](https://www.frontiersin.org/journals/pharmacology/articles/10.3389/fphar.2024.1380231/full)
2. [The Pharma Legal Handbook: Saudi Arabia](https://pharmaboardroom.com/legal-reports/the-pharma-legal-handbook-saudi-arabia/)
3. [CMS expert guide: advertising of medicines and medical devices - Saudi Arabia](https://cms.law/en/int/expert-guides/cms-expert-guide-to-advertising-of-medicines-and-medical-devices/saudi-arabia)
4. [Pharmaceutical Advertising and Public Perceptions in Saudi Arabia (PMC)](https://pmc.ncbi.nlm.nih.gov/articles/PMC11503259/)
5. [SDAIA: Guide to the Saudi Personal Data Protection Law](https://dgp.sdaia.gov.sa/wps/wcm/connect/f579bc32-fda8-47bd-bc6f-66b8cb77985c/ENG-Guide+to+the+saudi+PDP+law+for+controllersprocessors.pdf?MOD=AJPERES) · [PDPL Implementing Regulations](https://sdaia.gov.sa/en/SDAIA/about/Documents/ImplementingRegulation.pdf)
6. [Health Professions Practice Law (MOH)](https://www.moh.gov.sa/Documents/Practice-system.pdf)
7. [SFDA Registration Rules (v4)](https://www.sfda.gov.sa/sites/default/files/2023-06/RegistrationRulesV4E_0.pdf) · [Implementing Regulations of the Law of Pharmaceutical Establishments](https://www.sfda.gov.sa/sites/default/files/2025-05/SFDA28122020ee1.pdf)
8. [SFDA pricing rules (1442 AH)](https://www.eversana.com/wp-content/uploads/dlm_uploads/2021/01/Saudi-Arabia-Pricing-Guidelines.pdf) · [SFDA fines pharmacies (RSD tracking), 2026](https://www.lexismiddleeast.com/news/2026-01-14_37/en)
9. [Codeine / controlled medicines and online pharmacies in Saudi Arabia (Springer)](https://link.springer.com/article/10.1007/s11469-021-00670-4) · [SFDA controlled medicine permits system (2025)](https://www.lexismiddleeast.com/news/2025-08-18_23)
10. [Al Tamimi: e-commerce in Saudi Arabia](https://www.tamimi.com/law-update-articles/theres-something-in-your-cart-an-update-on-e-commerce-in-saudi-arabia) · [Saudipedia: E-commerce Law](https://saudipedia.com/en/e-commerce-law-in-saudi-arabia)
11. [Dispensing medications without prescription at Saudi community pharmacies](https://www.sciencedirect.com/science/article/pii/S1319016411001071)
12. [SDAIA generative AI guidelines (DataGuidance)](https://www.dataguidance.com/news/saudi-arabia-sdaia-publishes-guidelines-generative-ai)
13. [SFDA MDS-G010: AI/ML-based medical devices](https://www.sfda.gov.sa/sites/default/files/2023-01/MDS-G010ML.pdf) · [Regulating AI-based medical devices in Saudi Arabia (PMC)](https://pmc.ncbi.nlm.nih.gov/articles/PMC11250741/)
