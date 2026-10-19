{
    "name": "Pharmacy AI Agent",
    "version": "18.0.1.6.0",
    "summary": "Scheduled-medication flags + customer prescription (image/OCR) model for the AI ordering agent",
    "description": "Adds controlled/scheduled-substance classification to products and a "
                   "pharmacy.prescription model (image + OCR extraction + pharmacist "
                   "verification) that the WhatsApp ordering agent and OCR pipeline populate. "
                   "Dummy/testing use; real dispensing of scheduled meds always requires a human.",
    "author": "Pharmacy AI Agent project",
    "license": "LGPL-3",
    "category": "Inventory/Pharmacy",
    "depends": ["product", "stock", "sale_management"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/ir.model.access.csv",
        "views/pharmacy_views.xml",
        "views/product_views.xml",
        "data/sfda_sync_data.xml",
        "views/sfda_sync_views.xml",
    ],
    "application": True,
    "installable": True,
}
