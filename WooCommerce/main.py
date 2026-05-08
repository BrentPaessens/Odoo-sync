"""
main.py
───────
WooCommerce ↔ Odoo Synchronization

Unified entry point for:
  - Order import → Odoo sale.order (draft quotation)
  - Product sync from Odoo → WooCommerce

Modes:
  python main.py {mode} {option}
  python main.py --orders --once       → Order import (single run)
  python main.py --products            → Product sync
  Test commands:
  python main.py --products --dry-run       → Show what would happen
  python main.py --orders --dry-run
"""

import argparse
import json
import logging
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

try:
    import schedule
except ModuleNotFoundError:
    schedule = None

# Import local config first (has settings instance loaded from WooCommerce/.env)
from config import settings

# Add parent folder to path for shared modules (append keeps current dir first)
sys.path.append(str(Path(__file__).parent.parent))

from shared.models import (
    CompanyWooSyncConfig,
    OdooProduct,
    WooMetaData,
    WooOrder,
    SyncStatus,
)
from shared.odoo_controller import OdooController
from shared.customer_verification import CustomerVerifier
from woo_controller import WooController
from mapper import OrderMapper, map_odoo_to_woo

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ── Settings ──────────────────────────────────────────────────────────────────
# Default interval (fallback if company config not available)
DEFAULT_INTERVAL_MINUTES = 15
LOG_FILE = Path(__file__).parent / "error_log.txt"
SEP = "─" * 64
VAT_META_KEYS = (
    "_vat_number",
    "_billing_vat",
    "billing_vat",
    "vat_number",
    "eu_vat_number",
    "_wc_billing_vat",
)
SYNC_EVENT_MARKER = "SYNC_EVENT|"


def log_error_to_file(exc: Exception) -> None:
    """Schrijft kritieke fout met traceback naar error_log.txt."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n{'=' * 60}\n")
        f.write(f"KRITIEKE FOUT – {timestamp}\n")
        f.write(f"{'=' * 60}\n")
        f.write(traceback.format_exc())
        f.write("\n")
    logger.error("Fout weggeschreven naar: %s", LOG_FILE)


def emit_sync_event(event: str, **payload) -> None:
    """Emit machine-readable sync events to stdout log stream for Odoo ingestion."""
    event_payload = {
        "event": event,
        "run_id": os.environ.get("WOO_SYNC_RUN_ID"),
        "sync_type": os.environ.get("WOO_SYNC_TYPE"),
        "trigger_mode": os.environ.get("WOO_SYNC_TRIGGER_MODE"),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
    event_payload.update(payload)
    logger.error("%s%s", SYNC_EVENT_MARKER, json.dumps(event_payload, ensure_ascii=True))


# ════════════════════════════════════════════════════════════════════════════
# MULTI-COMPANY SCHEDULER
# ════════════════════════════════════════════════════════════════════════════

def load_company_configs(odoo: OdooController) -> list:
    """
    Load WooCommerce sync configurations for all active Odoo companies.
    
    Returns list of CompanyWooSyncConfig objects.
    """
    from shared.models import CompanyWooSyncConfig
    
    try:
        if odoo.uid is None:
            odoo.authenticate()
        
        company_responses = odoo.get_all_active_companies()
        configs = [resp.to_config() for resp in company_responses]
        
        logger.info("Geladen %s bedrijfsconfiguratie(s) uit Odoo", len(configs))
        for config in configs:
            logger.info(
                "  - %s (mode=%s, order=%s min, product=%s min)",
                config.company_name,
                config.woo_sync_interval_mode.value,
                config.sync_interval_minutes or "N/A",
                config.product_sync_interval_minutes or "N/A",
            )
        
        return configs
    except Exception as exc:
        logger.error("Error loading company configs: %s", exc)
        return []


# def run_scheduler_multi_company(after: str | None = None) -> None:
#     """
#     Multi-company scheduler supporting per-company cron intervals and settings.
    
#     Each company can have:
#     - Different intervals (15 min, 30 min, hourly, etc.)
#     - Disabled sync (manual-only mode)
#     - Different WooCommerce credentials (if specified)
#     - Stock tracking enabled/disabled per company
#     """
    
#     logger.info("╔════════════════════════════════════════════════════════╗")
#     logger.info("║ Multi-Company WooCommerce ↔ Odoo Scheduler             ║")
#     logger.info("╚════════════════════════════════════════════════════════╝")
    
#     # Load company configurations from Odoo
#     odoo = OdooController()
#     odoo.authenticate()
#     company_configs = load_company_configs(odoo)
    
#     if not company_configs:
#         logger.warning("Geen bedrijfsconfiguraties gevonden – uitgang.")
#         return
    
#     # Validate and filter for sync-enabled companies.
#     # Also require complete Woo credentials; otherwise skip the company.
#     active_configs: list[CompanyWooSyncConfig] = []
#     for cfg in company_configs:
#         if not cfg.woo_wordpress_plugin_enabled:
#             logger.info(
#                 "Bedrijf '%s' overgeslagen: Wordpress Plugin staat uit.",
#                 cfg.company_name,
#             )
#             continue
#         if not cfg.woo_sync_enabled:
#             continue
#         if not _has_required_woo_credentials(cfg):
#             logger.warning(
#                 "Bedrijf '%s' overgeslagen: ontbrekende Woo credentials (url/key/secret).",
#                 cfg.company_name,
#             )
#             continue
#         if cfg.sync_interval_minutes is None and cfg.product_sync_interval_minutes is None:
#             continue
#         active_configs.append(cfg)
    
#     if not active_configs:
#         logger.warning("Geen bedrijven met ingeschakelde auto-sync – uitgang.")
#         logger.info("(Voer manueel uit met: python main.py --orders --once)")
#         return
    
#     logger.info("")
#     logger.info("Starten met %d actief(e) bedrijf(ven)...", len(active_configs))
    
#     _stop = [False]
    
#     def create_order_job(company_config):
#         """Factory function for an order sync job for a specific company."""
#         def company_job():
#             logger.info(
#                 "── Order sync gestart voor bedrijf '%s' (interval=%s min)",
#                 company_config.company_name,
#                 company_config.sync_interval_minutes,
#             )
#             try:
#                 run_order_sync(after=after, company_config=company_config)
#             except Exception as exc:
#                 logger.exception(
#                     "Fout in order sync voor bedrijf '%s'",
#                     company_config.company_name,
#                 )
#                 log_error_to_file(exc)
#                 _stop[0] = True

#         return company_job

#     def create_product_job(company_config):
#         """Factory function for a product sync job for a specific company."""
#         def company_job():
#             logger.info(
#                 "── Product sync gestart voor bedrijf '%s' (interval=%s min)",
#                 company_config.company_name,
#                 company_config.product_sync_interval_minutes,
#             )
#             try:
#                 run_product_sync(company_id=company_config.company_id)
#             except Exception as exc:
#                 logger.exception(
#                     "Fout in product sync voor bedrijf '%s'",
#                     company_config.company_name,
#                 )
#                 log_error_to_file(exc)
#                 _stop[0] = True

#         return company_job

#     def create_combined_job(company_config):
#         """Factory function for a shared order/product sync job."""
#         def company_job():
#             logger.info(
#                 "── Gedeelde sync gestart voor bedrijf '%s' (interval=%s min)",
#                 company_config.company_name,
#                 company_config.sync_interval_minutes,
#             )
#             try:
#                 run_order_sync(after=after, company_config=company_config)
#                 run_product_sync(company_id=company_config.company_id)
#             except Exception as exc:
#                 logger.exception(
#                     "Fout in gedeelde sync voor bedrijf '%s'",
#                     company_config.company_name,
#                 )
#                 log_error_to_file(exc)
#                 _stop[0] = True

#         return company_job
    
#     if schedule is not None:
#         # Schedule jobs for each company with their configured interval setup
#         for config in active_configs:
#             if config.is_separate_sync_intervals:
#                 order_interval = config.sync_interval_minutes
#                 if order_interval and order_interval > 0:
#                     schedule.every(order_interval).minutes.do(create_order_job(config))
#                     logger.info(
#                         "Ingepland: %s – orders elke %d minuten",
#                         config.company_name,
#                         order_interval,
#                     )

#                 product_interval = config.product_sync_interval_minutes
#                 if product_interval and product_interval > 0:
#                     schedule.every(product_interval).minutes.do(create_product_job(config))
#                     logger.info(
#                         "Ingepland: %s – producten elke %d minuten",
#                         config.company_name,
#                         product_interval,
#                     )
#             else:
#                 interval = config.sync_interval_minutes
#                 if interval and interval > 0:
#                     schedule.every(interval).minutes.do(create_combined_job(config))
#                     logger.info(
#                         "Ingepland: %s – gedeelde sync elke %d minuten",
#                         config.company_name,
#                         interval,
#                     )
        
#         logger.info("")
#         logger.info("Alle jobs ingepland. Wacht op volgende sync...\n")
        
#         # Run initial sync for all companies using the same mode as the scheduler.
#         logger.info("First run – syncing all companies...")
#         for config in active_configs:
#             logger.info("Initial sync: %s", config.company_name)
#             try:
#                 if config.is_separate_sync_intervals:
#                     if config.sync_interval_minutes and config.sync_interval_minutes > 0:
#                         run_order_sync(after=after, company_config=config)
#                     if config.product_sync_interval_minutes and config.product_sync_interval_minutes > 0:
#                         run_product_sync(company_id=config.company_id)
#                 else:
#                     if config.sync_interval_minutes and config.sync_interval_minutes > 0:
#                         run_order_sync(after=after, company_config=config)
#                         run_product_sync(company_id=config.company_id)
#             except Exception as exc:
#                 logger.exception("Error in initial sync for %s", config.company_name)
#                 log_error_to_file(exc)
#                 _stop[0] = True
        
#         # Main scheduler loop
#         while not _stop[0]:
#             try:
#                 schedule.run_pending()
#                 time.sleep(1)
#             except KeyboardInterrupt:
#                 logger.info("")
#                 logger.info("Scheduler gestopt door gebruiker.")
#                 break
#     else:
#         logger.error("schedule module niet geïnstalleerd. Kan scheduler niet starten.")
#         logger.info("Voer eenmalig uit met: python main.py --orders --once")


# ══════════════════════════════════════════════════════════════════════════════
# ORDER IMPORT MODE – WooCommerce → Odoo
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class OrderSyncSummary:
    total_fetched: int = 0
    skipped_not_succeeded: int = 0
    skipped_duplicate: int = 0
    created: int = 0
    errors: list[str] = field(default_factory=list)


def _extract_vat_number(woo_order: WooOrder) -> str | None:
    """Haal BTW-nummer op uit billing of bekende WooCommerce meta keys."""
    billing_vat = (woo_order.billing.vat or "").strip()
    if billing_vat:
        return billing_vat

    for key in VAT_META_KEYS:
        value = woo_order.get_meta(key)
        if value and str(value).strip():
            return str(value).strip()

    return None


def _set_or_add_vat_meta(woo_order: WooOrder, vat_number: str) -> None:
    """Werk BTW-meta bij zodat verificatie consistent dezelfde key kan lezen."""
    updated = False
    for meta in woo_order.meta_data:
        if meta.key in VAT_META_KEYS:
            meta.value = vat_number
            updated = True

    if not updated:
        woo_order.meta_data.append(WooMetaData(key="_vat_number", value=vat_number))


def _apply_b2b_test_profile(woo_order: WooOrder) -> None:
    """Forceer demo B2B-data voor testorders om customer/delivery flow te valideren."""
    test_mode = bool(getattr(settings, "woo_b2b_test_mode", False))
    if not test_mode:
        return

    target_email = str(getattr(settings, "woo_b2b_test_email", "")).strip().lower()
    if not target_email:
        logger.warning(
            "WOO_B2B_TEST_MODE=true maar WOO_B2B_TEST_EMAIL ontbreekt; testprofiel wordt overgeslagen."
        )
        return

    order_email = (woo_order.billing.email or "").strip().lower()
    if order_email != target_email:
        return

    demo_company = str(getattr(settings, "woo_b2b_test_company", "")).strip()
    if demo_company:
        woo_order.billing.company = demo_company

    demo_street = str(getattr(settings, "woo_b2b_test_street", "")).strip()
    if demo_street:
        woo_order.billing.address_1 = demo_street

    demo_city = str(getattr(settings, "woo_b2b_test_city", "")).strip()
    if demo_city:
        woo_order.billing.city = demo_city

    demo_postcode = str(getattr(settings, "woo_b2b_test_postcode", "")).strip()
    if demo_postcode:
        woo_order.billing.postcode = demo_postcode

    demo_country = str(getattr(settings, "woo_b2b_test_country", "")).strip()
    if demo_country:
        woo_order.billing.country = demo_country

    vat_number = str(getattr(settings, "woo_b2b_test_vat", "")).strip()
    if vat_number:
        woo_order.billing.vat = vat_number
        _set_or_add_vat_meta(woo_order, vat_number)

    logger.info(
        "   B2B testprofiel actief voor order #%s (email=%s, company=%s, vat=%s)",
        woo_order.number,
        woo_order.billing.email,
        woo_order.billing.company or "-",
        vat_number or "-",
    )

# hulp functie
def _add_paid_note_to_order(odoo: OdooController, order_id: int, woo_order: WooOrder) -> None:
    """Voeg een interne notitie toe aan de sale.order dat de order betaald is in WooCommerce."""
    note = (
        f"Betaald via WooCommerce (methode: {woo_order.payment_method_title or 'onbekend'}). "
        f"Auto-confirm staat UIT — order blijft in draft."
    )
    try:
        if odoo._is_json2():
            odoo._json2_call_method(
                "sale.order",
                "message_post",
                ids=[order_id],
                kwargs={"body": note, "message_type": "comment", "subtype_xmlid": "mail.mt_note"},
            )
        else:
            odoo._call_kw(
                "sale.order",
                "message_post",
                [[order_id]],
                {"body": note, "message_type": "comment", "subtype_xmlid": "mail.mt_note"},
            )
        logger.info("Interne notitie toegevoegd aan order id=%s (betaald, draft)", order_id)
    except Exception as exc:
        logger.warning("Kon interne notitie niet toevoegen aan order id=%s: %s", order_id, exc)

def process_order(
    woo_order: WooOrder,
    odoo: OdooController,
    woo: WooController,
    dry_run: bool,
    summary: OrderSyncSummary,
    customer_verifier: CustomerVerifier | None = None,
    company_config: CompanyWooSyncConfig | None = None,
) -> None:
    """Maakt een offerte (draft sale.order) aan in Odoo voor een WooCommerce order."""

    intention_status = woo_order.get_meta("_intention_status")
    if intention_status is not None and intention_status != "succeeded":
        logger.info(
            "   SKIP order #%s – _intention_status=%s (betaling niet geslaagd)",
            woo_order.number,
            intention_status,
        )
        summary.skipped_not_succeeded += 1
        return

    status = woo_order.status
    total = float(woo_order.total)

    logger.info(
        "── Order #%s  status=%-12s  totaal=%.2f %s  email=%s",
        woo_order.number,
        status,
        total,
        woo_order.currency,
        woo_order.billing.email,
    )

    _apply_b2b_test_profile(woo_order)
    vat_number = _extract_vat_number(woo_order)
    if vat_number and not woo_order.billing.vat:
        woo_order.billing.vat = vat_number

    # DEBUG: Log exactly what WooCommerce API returned for this order
    logger.info(
        "   DEBUG WC API ADRESSEN – Order #%s"
        "\n      Billing:  %s, %s %s"
        "\n      Shipping: %s, %s %s",
        woo_order.number,
        woo_order.billing.address_1 or "(GEEN)",
        woo_order.billing.city or "(GEEN)",
        woo_order.billing.postcode or "(GEEN)",
        woo_order.shipping.address_1 if woo_order.shipping else "(GEEN SHIPPING OBJ)",
        woo_order.shipping.city if woo_order.shipping else "(GEEN)",
        woo_order.shipping.postcode if woo_order.shipping else "(GEEN)",
    )

    billing = woo_order.billing
    customer_name = billing.company or f"{billing.first_name} {billing.last_name}".strip()
    if not customer_name:
        raise ValueError(
            f"Order #{woo_order.number} mist een klantnaam. Een klant kan niet automatisch worden aangemaakt zonder naam."
        )
    if not billing.email.strip():
        raise ValueError(
            f"Order #{woo_order.number} mist een factuur-e-mailadres. Een klant kan niet automatisch worden aangemaakt zonder e-mail."
        )
    if not billing.address_1.strip() or not billing.city.strip() or not billing.postcode.strip() or not billing.country.strip():
        raise ValueError(
            f"Order #{woo_order.number} mist verplichte factuuradresgegevens. Een klant kan niet automatisch worden aangemaakt zonder volledig adres."
        )

    if dry_run:
        logger.info(
            "   [dry-run] Zou offerte aanmaken voor '%s %s' (%s)",
            billing.first_name,
            billing.last_name,
            billing.email,
        )
        for item in woo_order.line_items:
            logger.info(
                "   [dry-run]   %dx %-40s SKU=%-12s  @ %.2f",
                item.quantity,
                item.name,
                item.sku or "(geen SKU)",
                float(item.total) / max(item.quantity, 1),
            )
        return

    # ── CUSTOMER VERIFICATION (with enriched WC customer data) ───────
    # Fetch enriched customer data from WC endpoint before verification
    woo_customer_enriched = None
    if woo_order.customer_id and woo_order.customer_id > 0:
        try:
            woo_customer_enriched = woo.get_customer(woo_order.customer_id)
            if woo_customer_enriched:
                enriched_email = (woo_customer_enriched.get("email") or "").strip()
                enriched_first_name = (woo_customer_enriched.get("first_name") or "").strip()
                enriched_last_name = (woo_customer_enriched.get("last_name") or "").strip()
                enriched_phone = (
                    (woo_customer_enriched.get("billing") or {}).get("phone")
                    or woo_customer_enriched.get("phone")
                    or ""
                ).strip()
                enriched_company = (woo_customer_enriched.get("company_name") or "").strip()
                enriched_vat = (woo_customer_enriched.get("vat_number") or "").strip()

                # Use customer endpoint as source of truth for customer identity fields.
                if enriched_email and not billing.email:
                    billing.email = enriched_email
                if enriched_first_name and not billing.first_name:
                    billing.first_name = enriched_first_name
                if enriched_last_name and not billing.last_name:
                    billing.last_name = enriched_last_name
                if enriched_phone and not billing.phone:
                    billing.phone = enriched_phone
                # Company en VAT zijn B2B-indicators: wel altijd overnemen als enriched data het heeft
                if enriched_company and not billing.company:
                    billing.company = enriched_company
                if enriched_vat and not billing.vat:
                    billing.vat = enriched_vat

                customer_name = billing.company or f"{billing.first_name} {billing.last_name}".strip() or customer_name

                logger.info(
                    "   Enriched customer data fetched – email=%s company=%s vat=%s",
                    billing.email or "(geen)",
                    billing.company or "(geen)",
                    billing.vat or "(geen)",
                )
        except Exception as exc:
            logger.warning(
                "   Could not fetch enriched customer data for ID=%s: %s – continuing with billing address data",
                woo_order.customer_id,
                exc,
            )

    is_company = None

    # Verify customer before processing
    if customer_verifier:
        verification = customer_verifier.verify_woo_order_customer(
            woo_customer_id=woo_order.customer_id or 0,
            woo_email=billing.email,
            woo_name=customer_name,
            woo_phone=billing.phone,
            woo_company=billing.company,
            woo_vat=vat_number,
            billing_address=billing,
            shipping_address=woo_order.shipping,
            woo_customer=woo_customer_enriched,  # Pass enriched customer data
        )

        logger.info(
            "   Customer verification: %s (confidence=%.0f%%) → action=%s",
            verification.verification_status,
            verification.classification.confidence_percentage,
            verification.recommended_action,
        )

        # No more manual_review_needed routing – deterministic now: link_existing or create_new
        if verification.verification_status == "auto_matched":
            logger.info(
                "   Customer matched to existing Odoo partner id=%s",
                verification.recommended_partner_id,
            )
        elif verification.verification_status == "new_customer":
            if getattr(verification.classification, "customer_type", None) == "bedrijf":
                is_company = True
            logger.info(
                "   New customer will be created – type=%s (company=%s vat=%s)",
                verification.classification.customer_type if hasattr(verification.classification, 'customer_type') else "(unknown)",
                woo_customer_enriched.get("company_name", "(geen)") if woo_customer_enriched else "(geen)",
                woo_customer_enriched.get("vat_number", "(geen)") if woo_customer_enriched else "(geen)",
            )

    company_id = company_config.company_id if company_config else None

    if odoo.order_exists(woo_order.number, company_id=company_id):
        logger.info(
            "   SKIP order #%s – sale.order bestaat al in Odoo",
            woo_order.number,
        )
        summary.skipped_duplicate += 1
        return

    try:
        synced_to_woo = False
        product_map: dict[str, int] = {}
        for item in woo_order.line_items:
            sku = (item.sku or "").strip()
            if not sku and item.product_id == 0:
                raise ValueError(
                    f"Orderregel '{item.name}' heeft geen SKU en product_id=0. "
                    "Importeer het product eerst via Product sync."
                )
            if sku and sku not in product_map:
                pid = odoo.find_product_by_sku(sku, company_id=company_id)
                if pid is None:
                    raise ValueError(
                        f"Product SKU '{sku}' ({item.name}) niet gevonden in Odoo. "
                        "Importeer het product eerst via Product sync."
                    )
                product_map[sku] = pid
                logger.info("   SKU '%s' gevonden → product id=%s", sku, pid)

        customer_partners = odoo.resolve_customer_partners(
            billing,
            customer_name,
            shipping=woo_order.shipping,
            company_id=company_id,
            is_company=is_company,
            create_delivery_children=(
                company_config.woo_create_delivery_addresses if company_config else True
            ),
        )
        
        # DEBUG: Log address comparison for delivery child decision
        if woo_order.shipping:
            billing_key = f"{(billing.address_1 or '').strip()}|{(billing.city or '').strip()}|{(billing.postcode or '').strip()}"
            shipping_key = f"{(woo_order.shipping.address_1 or '').strip()}|{(woo_order.shipping.city or '').strip()}|{(woo_order.shipping.postcode or '').strip()}"
            match_status = "IDENTIEK" if billing_key == shipping_key else "VERSCHILLEND"
            logger.info(
                "   DEBUG ADRESVERGLEICH – Order #%s [%s]\n      Billing:  %s\n      Shipping: %s",
                woo_order.number,
                match_status,
                billing_key,
                shipping_key,
            )
        
        partner_id = customer_partners["partner_id"]
        shipping_partner_id = customer_partners["partner_shipping_id"]

        currency_id = odoo.get_currency_id(woo_order.currency)
        sale_vals, order_lines = OrderMapper.map(
            woo_order,
            partner_id,
            currency_id,
            product_map,
            shipping_partner_id=shipping_partner_id,
        )

        # Ensure order is created in the intended Odoo company.
        if company_id:
            sale_vals["company_id"] = company_id

        # Create order header (without lines)
        order_id = odoo.create_sale_order(sale_vals)
        logger.info(
            "   Offerte id=%s aangemaakt voor WooCommerce Order #%s",
            order_id,
            woo_order.number,
        )
        
        # Create order lines separately (JSON-2 compatible)
        if order_lines:
            try:
                line_ids = odoo.create_sale_order_lines(
                    order_id,
                    order_lines,
                    company_id=company_id,
                )
                logger.info(
                    "   Order #%s → %d order lines aangemaakt (ids: %s)",
                    woo_order.number,
                    len(line_ids),
                    line_ids,
                )
            except Exception as exc:
                logger.error(
                    "   Order #%s → Fout bij aanmaken order lines: %s",
                    woo_order.number,
                    exc,
                )
                raise RuntimeError(
                    f"Order lines aanmaken mislukt voor Odoo order id={order_id}: {exc}"
                ) from exc
        
        # Fetch S-number from Odoo and update WooCommerce with comprehensive sync status
        s_number = odoo.get_sale_order_number(order_id)
        if s_number:
            if woo.update_order_sync_status(
                woo_order.id,
                s_number=s_number,
                status="synced",
                partner_id=partner_id
            ):
                synced_to_woo = True
                # Update sync metadata in memory
                woo_order.sync_metadata.odoo_s_number = s_number
                woo_order.sync_metadata.odoo_partner_id = partner_id
                woo_order.sync_metadata.status = SyncStatus.SYNCED
                woo_order.sync_metadata.last_sync_success = datetime.now()
                logger.info(
                    "   Order #%s → Odoo S-nummer %s synced to WooCommerce",
                    woo_order.number,
                    s_number,
                )
                
                # ── AUTO-CONFIRM PAID ORDERS ────────────────────────────────────
                auto_confirm = company_config.woo_auto_confirm_paid_orders if company_config else False

                logger.info("DEBUG auto_confirm=%s, is_paid=%s", auto_confirm, woo_order.is_paid)

                # If payment has been collected, automatically confirm as sales order
                if woo_order.is_paid and auto_confirm:
                    try:
                        odoo.confirm_order(order_id)
                        woo_order.sync_metadata.is_sales_order = True
                        logger.info(
                            "   Order #%s → Paid order auto-confirmed as sales.order (status=sale)",
                            woo_order.number,
                        )

                        # -- Leverbon aanamaken ------
                        create_picking = company_config.woo_create_delivery_picking if company_config else False
                        track_stock    = company_config.woo_track_stock if company_config else False

                        if create_picking:
                            try:
                                picking_id = odoo.create_delivery_picking(order_id, reserve_stock=track_stock)
                                if picking_id:
                                    logger.info(
                                        "Order #%s → leverbon id=%s aangemaakt (stock_reservering=%s)",
                                        woo_order.number, picking_id, track_stock,
                                    )
                                else:
                                    logger.warning("Order #%s → leverbon aanmaken mislukt (geen id)", woo_order.number)
                            except Exception as picking_error:
                                logger.warning("Order #%s → leverbon fout: %s", woo_order.number, picking_error)
                        else:
                            logger.info("Order #%s → geen leverbon (Delivery Picking Flow staat uit)", woo_order.number)
                                                
                        # Try to update WooCommerce with sales order confirmation info
                        try:
                            woo.update_order_meta(
                                woo_order.id,
                                meta_key="_odoo_is_sales_order",
                                meta_value="yes"
                            )
                        except Exception as e:
                            logger.warning(
                                "   Could not update WooCommerce meta for sales order flag: %s", e
                            )
                    except Exception as confirm_error:
                        logger.error(
                            "   Order #%s → PHASE 7: Failed to confirm as sales order: %s",
                            woo_order.number,
                            confirm_error,
                        )
                        # Don't fail the entire sync, just log the error
                        woo_order.sync_metadata.sync_error_message = f"Confirmation failed: {confirm_error}"
                elif woo_order.is_paid and not auto_confirm:
                    _add_paid_note_to_order(odoo, order_id, woo_order)



                # ── PHASE 8: DELIVERY ADDRESS RESOLUTION ───────────────────────────────
                if shipping_partner_id != partner_id:
                    logger.info(
                        "   Order #%s → delivery partner resolved to child contact id=%s",
                        woo_order.number,
                        shipping_partner_id,
                    )
            else:
                logger.warning(
                    "   Order #%s → S-nummer %s opgehaald maar NIET naar WooCommerce geschreven",
                    woo_order.number,
                    s_number,
                )
        else:
            # Failed to retrieve S-number
            logger.warning(
                "   Order #%s → FOUT: S-nummer NIET opgehaald van Odoo order #%s",
                woo_order.number,
                order_id,
            )
            woo.update_order_sync_status(
                woo_order.id,
                status="failed",
                error_message=f"Failed to retrieve S-number from Odoo order {order_id}"
            )
        
        summary.created += 1
    
    except Exception as sync_error:
        # Log the error and mark order as failed/retry in WooCommerce
        error_msg = str(sync_error)
        logger.error(
            "   Order #%s – SYNC FOUT: %s",
            woo_order.number,
            error_msg,
        )

        if synced_to_woo:
            logger.warning(
                "   Order #%s – order is al als synced geschreven; retry status wordt overgeslagen.",
                woo_order.number,
            )
            raise
        
        # Try to update WooCommerce with error status
        try:
            woo.update_order_sync_status(
                woo_order.id,
                status="retry",
                error_message=error_msg
            )
            logger.info(
                "   Order #%s – Retry status opgeslagen in WooCommerce",
                woo_order.number,
            )
        except Exception as woo_error:
            logger.error(
                "   Order #%s – FOUT bij schrijven retry status naar WooCommerce: %s",
                woo_order.number,
                woo_error,
            )
        
        # Re-raise to let run_order_sync handle the summary update
        raise


def _resolve_woo_settings(company_config: CompanyWooSyncConfig | None) -> tuple[str | None, str | None, str | None]:
    """Resolve Woo credentials with company override first, then .env fallback."""
    if company_config is None:
        return None, None, None

    return (
        company_config.woo_url,
        company_config.woo_consumer_key,
        company_config.woo_consumer_secret,
    )


def _pick_active_company_config(odoo: OdooController) -> CompanyWooSyncConfig | None:
    """Pick one active company config for ad-hoc runs outside scheduler mode."""
    configs = load_company_configs(odoo)
    active_configs = [
        cfg for cfg in configs
        if cfg.woo_wordpress_plugin_enabled and cfg.woo_sync_enabled
    ]
    if not active_configs:
        return None
    if len(active_configs) > 1:
        logger.warning(
            "Meerdere actieve bedrijven gevonden (%s). Eerste bedrijf '%s' wordt gebruikt.",
            len(active_configs),
            active_configs[0].company_name,
        )
    return active_configs[0]


def _has_required_woo_credentials(config: CompanyWooSyncConfig) -> bool:
    """Check whether company config has minimum Woo credentials for sync."""
    return bool(
        (config.woo_url or "").strip()
        and (config.woo_consumer_key or "").strip()
        and (config.woo_consumer_secret or "").strip()
    )


def _get_product_sync_company_config(
    odoo: OdooController,
    explicit_company_id: int | None,
) -> CompanyWooSyncConfig | None:
    """Resolve company config for product sync deterministically.

    Priority:
      1) --company argument
      2) WOO_COMPANY_ID environment variable
      3) exactly one eligible Woo company in Odoo

    Eligible company = woo_sync_enabled + required Woo credentials present.
    """
    if settings.woo_url.strip():
        # Legacy single-company mode via .env Woo credentials.
        return None

    target_company_id = explicit_company_id
    if target_company_id is None:
        company_env = os.environ.get("WOO_COMPANY_ID", "").strip()
        if company_env:
            try:
                target_company_id = int(company_env)
                logger.info("Company selectie via WOO_COMPANY_ID=%s", target_company_id)
            except ValueError as exc:
                raise RuntimeError(
                    f"WOO_COMPANY_ID moet een getal zijn, ontvangen: '{company_env}'"
                ) from exc

    if target_company_id is not None:
        responses = odoo.get_company_woo_sync_config(company_id=target_company_id)
        if not responses:
            raise RuntimeError(f"Company id={target_company_id} niet gevonden in Odoo.")

        company_config = responses[0].to_config()
        if not company_config.woo_wordpress_plugin_enabled:
            raise RuntimeError(
                f"Company '{company_config.company_name}' (id={company_config.company_id}) "
                "heeft de Wordpress Plugin uitgeschakeld."
            )
        if not company_config.woo_sync_enabled:
            raise RuntimeError(
                f"Company '{company_config.company_name}' (id={company_config.company_id}) "
                "heeft woo_sync_enabled uitgeschakeld."
            )
        if not _has_required_woo_credentials(company_config):
            raise RuntimeError(
                f"Company '{company_config.company_name}' (id={company_config.company_id}) "
                "mist Woo credentials (URL/CK/CS)."
            )
        return company_config

    all_configs = [
        cfg
        for cfg in load_company_configs(odoo)
        if cfg.woo_wordpress_plugin_enabled and cfg.woo_sync_enabled
    ]
    eligible_configs = [cfg for cfg in all_configs if _has_required_woo_credentials(cfg)]

    if not eligible_configs:
        raise RuntimeError(
            "Geen actief WooCommerce bedrijf met volledige credentials gevonden. "
            "Vul woo_url, woo_consumer_key en woo_consumer_secret in op Company A "
            "of gebruik --company <id>."
        )

    if len(eligible_configs) > 1:
        options = ", ".join(
            f"{cfg.company_id}:{cfg.company_name}" for cfg in eligible_configs
        )
        raise RuntimeError(
            "Meerdere WooCommerce bedrijven gevonden. "
            f"Gebruik --company <id> of WOO_COMPANY_ID. Beschikbaar: {options}"
        )

    return eligible_configs[0]


def _get_sync_orders(woo: WooController, after: str | None = None) -> list[WooOrder]:
    """Haal WooCommerce orders op die in de sync-flow moeten worden meegenomen."""
    orders_by_id: dict[int, WooOrder] = {}
    for status in ("processing", "pending", "on-hold"):
        for order in woo.get_orders(status=status, after=after):
            orders_by_id[order.id] = order

    return [orders_by_id[order_id] for order_id in sorted(orders_by_id)]


def run_order_sync(
    dry_run: bool = False,
    after: str | None = None,
    company_config: CompanyWooSyncConfig | None = None,
) -> None:
    """Voert één order synchronisatiecyclus uit."""

    emit_sync_event("run_start", sync_type="orders")

    if dry_run:
        logger.info("=== DRY RUN – geen wijzigingen in Odoo ===")

    if company_config is None and not settings.woo_url.strip():
        odoo_for_config = OdooController()
        odoo_for_config.authenticate()
        company_config = _pick_active_company_config(odoo_for_config)

    woo_url, woo_key, woo_secret = _resolve_woo_settings(company_config)
    woo = WooController(
        woo_url=woo_url,
        woo_consumer_key=woo_key,
        woo_consumer_secret=woo_secret,
    )
    woo_orders: list[WooOrder] = _get_sync_orders(woo, after=after)

    summary = OrderSyncSummary(total_fetched=len(woo_orders))
    logger.info("%s order(s) opgehaald uit WooCommerce.", summary.total_fetched)

    odoo = OdooController()
    if woo_orders or company_config is not None:
        odoo.authenticate()

    if company_config is not None and not dry_run:
        try:
            odoo.update_company_sync_status(
                company_id=company_config.company_id,
                status="syncing",
                message="Sync gestart door script.",
            )
        except Exception as exc:
            logger.warning("Kon company sync status niet op 'syncing' zetten: %s", exc)

    # ── PHASE 6: Initialize customer verification ───────────────────────────
    customer_verifier = None
    try:
        if woo_orders:
            # Fetch all Odoo customers for verification matching
            odoo_customers = odoo.get_all_customers()
            customer_verifier = CustomerVerifier(odoo_customers=odoo_customers)
            logger.info("Customer verifier initialized with %d Odoo customers", len(odoo_customers))
    except Exception as e:
        logger.error("Failed to initialize customer verifier: %s. Continuing without verification.", e)
        customer_verifier = None

    for woo_order in woo_orders:
        try:
            process_order(
                woo_order,
                odoo,
                woo,
                dry_run=dry_run,
                summary=summary,
                customer_verifier=customer_verifier,
                company_config=company_config,
            )
        except Exception as exc:
            msg = f"Order #{woo_order.number}: onverwachte fout – {exc}"
            logger.exception("   %s", msg)
            summary.errors.append(msg)
            emit_sync_event(
                "item_error",
                sync_type="orders",
                item_type="order",
                item_ref=str(woo_order.number),
                error=str(exc),
            )

    logger.info("")
    logger.info("══════════════════════ ORDER SYNC SAMENVATTING ══════════════════════")
    logger.info("  Opgehaald uit WooCommerce    : %s", summary.total_fetched)
    logger.info("  Overgeslagen (niet succesvol): %s", summary.skipped_not_succeeded)
    logger.info("  Overgeslagen (al aanwezig)   : %s", summary.skipped_duplicate)
    logger.info("  Offertes aangemaakt          : %s", summary.created)
    if summary.errors:
        logger.info("  Fouten                       : %s", len(summary.errors))
        for err in summary.errors:
            logger.error("    – %s", err)
    logger.info("═════════════════════════════════════════════════════════════════════")

    if company_config is not None and not dry_run:
        final_status = "failed" if summary.errors else "success"
        final_message = (
            f"Sync klaar. Opgehaald={summary.total_fetched}, aangemaakt={summary.created}, "
            f"fouten={len(summary.errors)}."
        )
        try:
            odoo.update_company_sync_status(
                company_id=company_config.company_id,
                status=final_status,
                message=final_message,
            )
        except Exception as exc:
            logger.warning("Kon company sync status niet bijwerken na afloop: %s", exc)

    emit_sync_event(
        "run_end",
        sync_type="orders",
        status="failed" if summary.errors else "success",
        total_fetched=summary.total_fetched,
        created=summary.created,
        skipped=summary.skipped_not_succeeded + summary.skipped_duplicate,
        error_count=len(summary.errors),
    )

# ══════════════════════════════════════════════════════════════════════════════
# PRODUCT SYNC MODE – Odoo → WooCommerce
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ProductSyncSummary:
    total: int = 0
    created: int = 0
    updated: int = 0
    removed: int = 0
    skipped_no_sku: int = 0
    errors: list[str] = field(default_factory=list)


def _connect_odoo() -> OdooController:
    """Maak verbinding met Odoo of stop met duidelijke foutmelding."""
    try:
        odoo = OdooController()
        odoo.authenticate()
        return odoo
    except Exception as exc:
        raise RuntimeError(f"Odoo verbinding mislukt: {exc}") from exc


def _connect_woo_and_load_skus(
    odoo: OdooController,
    company_config: CompanyWooSyncConfig | None = None,
) -> tuple[WooController, dict[str, int], CompanyWooSyncConfig | None]:
    """Maak verbinding met WooCommerce en laad bestaande SKU mapping."""
    if company_config is None and not settings.woo_url.strip():
        company_config = _pick_active_company_config(odoo)

    try:
        woo_url, woo_key, woo_secret = _resolve_woo_settings(company_config)
        woo = WooController(
            woo_url=woo_url,
            woo_consumer_key=woo_key,
            woo_consumer_secret=woo_secret,
        )
        sku_map = woo.get_all_skus()
        return woo, sku_map, company_config
    except Exception as exc:
        raise RuntimeError(f"WooCommerce verbinding mislukt: {exc}") from exc


def _sync_brands(woo: WooController, odoo_products: list[OdooProduct]) -> dict[str, int]:
    """Synchroniseer merken en geef actuele brand mapping terug."""
    print(f"\n[3/4] Merken synchroniseren...\n")
    try:
        brand_map = woo.get_all_brands()
        unique_brands = set()
        for product in odoo_products:
            if product.product_brand_id and len(product.product_brand_id) >= 2:
                brand_name = str(product.product_brand_id[1]).strip()
                if brand_name:
                    unique_brands.add(brand_name)

        for brand_name in sorted(unique_brands):
            woo.get_or_create_brand(brand_name, brand_map)

        print(f"  {len(brand_map)} merk(en) beschikbaar in WooCommerce.")
        return brand_map
    except Exception as exc:
        print(f"  WAARSCHUWING: Merken konden niet sync'en — {exc}")
        return {}


def _sync_single_product(
    product: OdooProduct,
    woo: WooController,
    brand_map: dict[str, int],
    sku_map: dict[str, int],
    dry_run: bool,
    summary: ProductSyncSummary,
) -> None:
    """Synchroniseer een individueel product naar WooCommerce."""
    sku = (product.default_code or "").strip()
    fallback_used = False

    if not sku:
        sku = f"ODOO-{product.template_id or product.id}"
        fallback_used = True

    payload = map_odoo_to_woo(product, brand_map)
    payload_dict = payload.model_dump(exclude_none=True)
    sale_label = payload_dict.get("sale_price", "")
    sale_display = sale_label if sale_label not in (None, "") else "-"
    price_info = f"RP={payload_dict.get('regular_price')} SP={sale_display}"

    if dry_run:
        action = "UPDATE" if sku in sku_map else "CREATE"
        suffix = " [fallback SKU]" if fallback_used else ""
        print(f"[dry-run] {action:6}   SKU={sku:<16}   {product.name}   {price_info}{suffix}")
        return

    if sku in sku_map:
        woo.update_product(sku_map[sku], payload_dict)
        summary.updated += 1
        suffix = " [fallback SKU]" if fallback_used else ""
        print(f"[UPDATE]  SKU={sku:<16}  {product.name}  {price_info}{suffix}")
    else:
        woo.create_product(payload_dict)
        summary.created += 1
        suffix = " [fallback SKU]" if fallback_used else ""
        print(f"[CREATE]  SKU={sku:<16}  {product.name}  {price_info}{suffix}")


def _product_sync_sku(product: OdooProduct) -> str:
    """Derive a stable SKU for WooCommerce sync."""
    sku = (product.default_code or "").strip()
    if sku:
        return sku
    return f"ODOO-{product.template_id or product.id}"


def _update_product_sync_progress(
    odoo: OdooController,
    company_id: int | None,
    phase: str,
    percent: float,
    current: int,
    total: int,
    message: str,
    status: str = "syncing",
    finished: bool = False,
) -> None:
    """Best-effort progress update to Odoo company fields."""
    if company_id is None:
        return
    try:
        odoo.update_company_sync_progress(
            company_id=company_id,
            phase=phase,
            percent=percent,
            current=current,
            total=total,
            status=status,
            message=message,
            finished=finished,
        )
    except Exception as exc:
        logger.debug("Kon sync progress niet bijwerken: %s", exc)


def _print_product_summary(summary: ProductSyncSummary) -> None:
    """Print eindsamenvatting van de productsync."""
    ascii_sep = "-" * 64
    print(f"\n{ascii_sep}")
    print(f"  Productsync afgerond")
    print(f"    Totaal     : {summary.total}")
    print(f"    Aangemaakt : {summary.created}")
    print(f"    Bijgewerkt : {summary.updated}")
    print(f"    Verwijderd : {summary.removed}")
    print(f"    Overgeslagen: {summary.skipped_no_sku}")
    print(f"    Fouten     : {len(summary.errors)}")
    if summary.errors:
        print(f"\n  Foutdetails:")
        for error in summary.errors:
            print(f"    - {error}")
    print(f"{ascii_sep}\n")


def run_product_sync(
    dry_run: bool = False,
    company_id: int | None = None,
    hard_delete_missing: bool = True,
    hard_delete_limit: int = 10,
) -> ProductSyncSummary:
    """Voert product synchronisatie van Odoo naar WooCommerce uit.
    
    Args:
        dry_run: Toon wat zou gebeuren zonder wijzigingen
        company_id: Odoo bedrijf ID voor product sync.
                   Als None: probeer auto-detect via omgevingsvariabele of first enabled company
        hard_delete_missing: Als True worden ontbrekende Woo producten permanent verwijderd
        hard_delete_limit: Veiligheidslimiet voor hard deletes per run
    """

    summary = ProductSyncSummary(total=0)
    emit_sync_event("run_start", sync_type="products")

    print(f"\n Verbinden met Odoo en WooCommerce...\n")
    try:
        odoo = _connect_odoo()
    except Exception as exc:
        summary.errors.append(str(exc))
        print(f"  {exc}")
        return summary

    try:
        selected_company = _get_product_sync_company_config(odoo, company_id)
    except Exception as exc:
        msg = f"Company selectie mislukt: {exc}"
        print(f"  {msg}")
        summary.errors.append(msg)
        return summary

    if selected_company is not None:
        company_id = selected_company.company_id
        print(
            "[Company lookup] Geselecteerd: "
            f"'{selected_company.company_name}' (ID: {selected_company.company_id})\n"
        )

    _update_product_sync_progress(
        odoo,
        company_id,
        phase="Verbinden met Odoo en WooCommerce",
        percent=10,
        current=0,
        total=0,
        message="Verbinden met Odoo en WooCommerce...",
    )

    try:
        woo, sku_map, _ = _connect_woo_and_load_skus(odoo, company_config=selected_company)
    except Exception as exc:
        summary.errors.append(str(exc))
        _update_product_sync_progress(
            odoo,
            company_id,
            phase="Verbinding mislukt",
            percent=100,
            current=0,
            total=0,
            message=str(exc),
            status="failed",
            finished=True,
        )
        return summary

    print(f"\n Producten ophalen uit Odoo...\n")
    _update_product_sync_progress(
        odoo,
        company_id,
        phase="Producten ophalen uit Odoo",
        percent=25,
        current=0,
        total=0,
        message=" Producten ophalen uit Odoo...",
    )
    try:
        odoo_products = odoo.get_products(company_id=company_id)
        logger.info(
            "Productsync filter: company_id=%s | producten=%s",
            company_id,
            len(odoo_products),
        )
    except Exception as exc:
        msg = f"Producten ophalen uit Odoo mislukt: {exc}"
        print(f"  {msg}")
        summary.errors.append(msg)
        _update_product_sync_progress(
            odoo,
            company_id,
            phase="Producten ophalen mislukt",
            percent=100,
            current=0,
            total=0,
            message=msg,
            status="failed",
            finished=True,
        )
        return summary

    summary.total = len(odoo_products)
    _update_product_sync_progress(
        odoo,
        company_id,
        phase="Producten geladen",
        percent=35,
        current=0,
        total=summary.total,
        message=f"{summary.total} product(en) geladen uit Odoo.",
    )

    brand_map = _sync_brands(woo, odoo_products)
    _update_product_sync_progress(
        odoo,
        company_id,
        phase="Merken synchroniseren",
        percent=45,
        current=0,
        total=summary.total,
        message=" Merken synchroniseren...",
    )

    print(f"\n Producten synchroniseren met WooCommerce...\n")

    for index, product in enumerate(odoo_products, start=1):
        try:
            _sync_single_product(
                product=product,
                woo=woo,
                brand_map=brand_map,
                sku_map=sku_map,
                dry_run=dry_run,
                summary=summary,
            )

        except Exception as exc:
            sku = (product.default_code or "").strip() or "(geen SKU)"
            msg = f"SKU={sku} ({product.name}): {exc}"
            summary.errors.append(msg)
            print(f"  [FOUT]    {msg}")
            emit_sync_event(
                "item_error",
                sync_type="products",
                item_type="product",
                item_ref=sku,
                error=str(exc),
            )

        percent = 45 + (50 * index / max(summary.total, 1))
        _update_product_sync_progress(
            odoo,
            company_id,
            phase="Producten synchroniseren",
            percent=percent,
            current=index,
            total=summary.total,
            message=f" Producten synchroniseren... ({index}/{summary.total})",
        )
        time.sleep(1)

    # Verwijder WooCommerce producten die niet meer actief in Odoo bestaan.
    print(f"\n[cleanup] Verwijderde Odoo-producten opruimen in WooCommerce...\n")
    active_odoo_skus = {
        _product_sync_sku(product)
        for product in odoo_products
    }
    stale_woo_products = {
        sku: woo_id
        for sku, woo_id in sku_map.items()
        if sku not in active_odoo_skus
    }

    if stale_woo_products:
        logger.info(
            "Cleanup: %s WooCommerce product(en) bestaan niet meer als actief product in Odoo",
            len(stale_woo_products),
        )
        if hard_delete_missing and not dry_run and len(stale_woo_products) > hard_delete_limit:
            msg = (
                "Cleanup gestopt: hard-delete limiet overschreden "
                f"({len(stale_woo_products)} > {hard_delete_limit}). "
                "Verhoog --hard-delete-limit als dit bewust is."
            )
            summary.errors.append(msg)
            print(f"  [FOUT]    {msg}")
            _print_product_summary(summary)
            _update_product_sync_progress(
                odoo,
                company_id,
                phase="Cleanup mislukt",
                percent=100,
                current=summary.total,
                total=summary.total,
                message=msg,
                status="failed",
                finished=True,
            )
            return summary

        for stale_sku, stale_woo_id in stale_woo_products.items():
            try:
                if dry_run:
                    action = "HARD-DELETE" if hard_delete_missing else "DELETE"
                    print(f"[dry-run] {action:10} SKU={stale_sku:<16}   woo_id={stale_woo_id}")
                    continue
                if hard_delete_missing:
                    woo.hard_delete_product(stale_woo_id)
                    print(f"[HARD-DELETE] SKU={stale_sku:<16}  woo_id={stale_woo_id}")
                else:
                    woo.trash_product(stale_woo_id)
                    print(f"[DELETE]  SKU={stale_sku:<16}  woo_id={stale_woo_id}")
                summary.removed += 1
            except Exception as exc:
                msg = f"SKU={stale_sku} (woo_id={stale_woo_id}): verwijderen mislukt: {exc}"
                summary.errors.append(msg)
                print(f"  [FOUT]    {msg}")
            time.sleep(0.5)
    else:
        logger.info("Cleanup: geen te verwijderen WooCommerce producten gevonden")

    _update_product_sync_progress(
        odoo,
        company_id,
        phase="Afronden",
        percent=98,
        current=summary.total,
        total=summary.total,
        message="Cleanup voltooid. Samenvatting opstellen...",
    )

    _print_product_summary(summary)
    if summary.errors:
        _update_product_sync_progress(
            odoo,
            company_id,
            phase="Mislukt",
            percent=100,
            current=summary.total,
            total=summary.total,
            message=(
                f"Productsync mislukt: {len(summary.errors)} fout(en), "
                f"created={summary.created}, updated={summary.updated}, removed={summary.removed}."
            ),
            status="failed",
            finished=True,
        )
        emit_sync_event(
            "run_end",
            sync_type="products",
            status="failed",
            total=summary.total,
            created=summary.created,
            updated=summary.updated,
            removed=summary.removed,
            error_count=len(summary.errors),
        )
    else:
        _update_product_sync_progress(
            odoo,
            company_id,
            phase="Voltooid",
            percent=100,
            current=summary.total,
            total=summary.total,
            message=(
                f"Productsync succesvol: created={summary.created}, "
                f"updated={summary.updated}, removed={summary.removed}."
            ),
            status="success",
            finished=True,
        )
        emit_sync_event(
            "run_end",
            sync_type="products",
            status="success",
            total=summary.total,
            created=summary.created,
            updated=summary.updated,
            removed=summary.removed,
            error_count=0,
        )
    return summary


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="WooCommerce ↔ Odoo synchronization",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --orders --once        Order import (single run)
  python main.py --products             Product sync (Odoo → WooCommerce)
  python main.py --dry-run --orders     Show what would be imported
  python main.py --dry-run --products   Show what would be synced
        """,
    )
    
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--orders",
        action="store_true",
        help="Import orders from WooCommerce into Odoo",
    )
    mode_group.add_argument(
        "--products",
        action="store_true",
        help="Sync products from Odoo to WooCommerce",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without making changes",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit (no scheduler for --orders mode)",
    )
    # krijg alleen orders gemaakt na bepaalde datum/tijd (ISO-8601), bijv: 2024-01-01T00:00:00
    parser.add_argument(
        "--after",
        metavar="DATETIME",
        help="For --orders: import only orders created after this time (ISO-8601)",
    )
    # specifieer odoo company id (optional)
    parser.add_argument(
        "--company",
        metavar="COMPANY_ID",
        type=int,
        help="Odoo company ID (for multi-company setups). Default: auto-detect first enabled company",
    )
    parser.add_argument(
        "--hard-delete-missing",
        action="store_true",
        help="For --products: verwijder ontbrekende Woo producten permanent (force=true). Dit is nu de standaard.",
    )
    parser.add_argument(
        "--trash-missing",
        action="store_true",
        help="For --products: zet ontbrekende Woo producten in de prullenbak in plaats van hard delete",
    )
    parser.add_argument(
        "--hard-delete-limit",
        metavar="N",
        type=int,
        default=10,
        help="For --products + --hard-delete-missing: max aantal hard deletes per run (default: 10)",
    )

    args = parser.parse_args()

    # ── PRODUCT SYNC MODE ─────────────────────────────────────────────────────
    if args.products:
        if args.hard_delete_limit < 1:
            print("  Ongeldige --hard-delete-limit: gebruik een waarde >= 1")
            sys.exit(1)
        cleanup_mode_hard_delete = True
        if args.trash_missing:
            cleanup_mode_hard_delete = False
        elif args.hard_delete_missing:
            cleanup_mode_hard_delete = True
        product_summary = run_product_sync(
            dry_run=args.dry_run,
            company_id=args.company,
            hard_delete_missing=cleanup_mode_hard_delete,
            hard_delete_limit=args.hard_delete_limit,
        )
        sys.exit(1 if product_summary.errors else 0)

    # ── ORDER SYNC MODE ───────────────────────────────────────────────────────
    elif args.orders:
        if args.dry_run or args.once:
            run_order_sync(dry_run=args.dry_run, after=args.after)
        else:
            # Multi-company scheduler (Phase 5)
            run_scheduler_multi_company(after=args.after)
