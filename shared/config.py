"""
config.py
─────────
Gedeeld tussen alle platformen (WooCommerce, Shopify, etc).
Elke platform heeft zijn eigen .env bestand in zijn map.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Basic settings shared across all platforms."""
    
    # ── Odoo ────────────────────────────────────────────────────────
    odoo_url: str = ""
    odoo_db: str = ""
    odoo_username: str = ""
    odoo_password: str = ""
    # Vereist voor Odoo v19+ (JSON-2).
    odoo_api_key: str = ""

    # ── WooCommerce ──────────────────────────────────────
    woo_url: str = ""
    woo_consumer_key: str = ""
    woo_consumer_secret: str = ""

    # ── Shopify ──────────────────────────────────────
    shopify_shop: str = ""           # bijv. store-a2026.myshopify.com
    shopify_access_token: str = ""   # shpat_xxxxxx

    # ── WooCommerce specifiek ────────────────────────────────────────
    odoo_payment_journal: str = "Bank"  # Naam van het Odoo journaal voor betalingen
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )
