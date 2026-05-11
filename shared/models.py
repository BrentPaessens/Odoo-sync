"""
models.py
─────────
Pydantic models for:
  - WooCommerce order data (as returned by the API)
  - Odoo product data
  - The internal StandardOrder format (ERP-agnostic)
  - WooCommerce payloads for product sync
"""

from __future__ import annotations
import re
from typing import Annotated, Any
from enum import Enum
from datetime import datetime
from pydantic import BaseModel, BeforeValidator, field_validator, model_validator

# Prijsvelden in WooCommerce kunnen string of float/int zijn; altijd naar str converteren
_StrVal = Annotated[str, BeforeValidator(str)]



# SYNC STATUS & METADATA (Order synchronization tracking)
class SyncStatus(str, Enum):
    """Synchronization status for orders between WooCommerce and Odoo."""
    PENDING = "pending"        # Order received, awaiting sync
    SYNCING = "syncing"        # Currently syncing to Odoo
    SYNCED = "synced"          # Successfully synced to Odoo
    FAILED = "failed"          # Sync failed, needs retry
    RETRY = "retry"            # Queued for retry


class OrderSyncMetadata(BaseModel):
    """Metadata tracking synchronization details for an order."""
    status: SyncStatus = SyncStatus.PENDING
    odoo_s_number: str | None = None        # Odoo sale order number (S-123)
    odoo_partner_id: int | None = None      # Odoo partner (customer) ID
    last_sync_attempt: datetime | None = None
    last_sync_success: datetime | None = None
    sync_error_message: str | None = None
    retry_count: int = 0
    is_sales_order: bool = False            # Phase 7: Order confirmed as sales order (not draft)
    
    @property
    def is_synced(self) -> bool:
        """Check if order has been successfully synced."""
        return self.status == SyncStatus.SYNCED
    
    @property
    def has_error(self) -> bool:
        """Check if sync has failed."""
        return self.status in (SyncStatus.FAILED, SyncStatus.RETRY)



# ODOO PRODUCT (Product sync)
class OdooProduct(BaseModel):
    """Velden zoals Odoo ze teruggeeft via de JSON-RPC API."""

    id: int
    name: str                              # Productnaam
    default_code: str | None = None        # SKU
    barcode: str | None = None             # EAN / barcode
    list_price: float = 0.0                # Verkoopprijs 
    sale_price: float | None = None         # Kortingprijs voor WooCommerce (indien geconfigureerd)
    discount_percent: float | None = None   # Percentage korting als alternatief voor vaste sale_price
    description_sale: str | None = None    # Verkoopbeschrijving
    qty_available: int                     # Voorraad
    categ_id: list[Any] | None = None      # [id, "Categorienaam"]
    product_brand_id: list[Any] | None = None  # [id, "Merknaam"]
    product_tmpl_id: list[Any] | int | None = None  # Variant → template mapping voor pricelistregels

    # Odoo stuurt False terug voor lege velden in plaats van None of []
    @field_validator("default_code", "description_sale", "product_brand_id", "barcode", mode="before")
    @classmethod
    def false_to_none(cls, v: Any) -> Any:
        return None if v is False else v

    @field_validator("categ_id", mode="before")
    @classmethod
    def false_to_none_list(cls, v: Any) -> Any:
        return None if v is False else v

    @property
    def template_id(self) -> int | None:
        """Geeft product template id terug, ongeacht JSON-RPC/JSON-2 many2one-formaat."""
        value = self.product_tmpl_id
        if isinstance(value, (list, tuple)) and value:
            try:
                return int(value[0])
            except (TypeError, ValueError):
                return None
        if isinstance(value, int):
            return value
        return None

    @property
    def resolved_description(self) -> str:
        """Geeft de beschrijving terug, of een lege string als die ontbreekt."""
        return self.description_sale or ""

    @property
    def brand_name(self) -> str | None:
        """Geeft de merknaam terug uit het Many2one veld product_brand_id."""
        if self.product_brand_id and len(self.product_brand_id) >= 2:
            return str(self.product_brand_id[1])
        return None



# WOOCOMMERCE ORDER (Order automation)
class WooMetaData(BaseModel):
    """One entry in a WooCommerce order's meta_data list."""

    key: str = ""
    value: str = ""


class WooBillingAddress(BaseModel):
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    vat: str = ""
    email: str = ""
    phone: str = ""
    address_1: str = ""
    address_2: str = ""
    city: str = ""
    state: str = ""
    postcode: str = ""
    country: str = ""


class WooLineItem(BaseModel):
    id: int
    name: str
    product_id: int = 0
    sku: str = ""
    quantity: int = 1
    price: _StrVal = "0"
    subtotal: _StrVal = "0"
    total: _StrVal = "0"
    subtotal_tax: _StrVal = "0"  # 0.00 → geen BTW koppelen


class WooShippingLine(BaseModel):
    id: int = 0
    method_title: str = ""
    total: str = "0"


class WooProductStock(BaseModel):
    id: int
    name: str
    sku: str = ""
    stock_quantity: int | None = None


class WooOrder(BaseModel):
    id: int
    number: str
    status: str
    date_created: str
    total: str
    currency: str = "EUR"
    customer_id: int | None = None     # WooCommerce customer ID (None for guest orders)
    payment_method: str = ""
    payment_method_title: str = ""
    transaction_id: str = ""           # Stripe Payment Intent ID
    date_paid: str | None = None       # ISO-8601 datetime when paid
    meta_data: list[WooMetaData] = []  # Stripe & WC Pay meta fields
    billing: WooBillingAddress
    shipping: WooBillingAddress | None = None  # Shipping address (if different from billing)
    line_items: list[WooLineItem] = []
    shipping_lines: list[WooShippingLine] = []
    
    # Synchronization tracking
    sync_metadata: OrderSyncMetadata = OrderSyncMetadata()

    _VAT_META_KEYS = (
        "_vat_number",
        "_billing_vat",
        "billing_vat",
        "vat_number",
        "eu_vat_number",
        "_wc_billing_vat",
    )
    _COMPANY_META_KEYS = (
        "_billing_company",
        "billing_company",
        "company_name",
        "company",
        "_company_name",
    )

    @model_validator(mode="after")
    def _populate_billing_from_meta(self) -> "WooOrder":
        vat_value = self.billing.vat.strip()
        if not vat_value:
            vat_value = self._find_meta_value(self._VAT_META_KEYS)
        if vat_value:
            self.billing.vat = vat_value

        company_value = self.billing.company.strip()
        if not company_value:
            company_value = self._find_meta_value(self._COMPANY_META_KEYS)
        if company_value:
            self.billing.company = company_value

        return self

    @property
    def is_paid(self) -> bool:
        """
        WooCommerce statuses that confirm payment has been collected.
        - processing  → paid, awaiting fulfilment (most common for Stripe / PayPal)
        - completed   → paid and fully fulfilled
        - shipped     → custom status some shops add after payment
        """
        return self.status in ("processing", "completed", "shipped")

    @property
    def reference(self) -> str:
        """Human-readable reference used as the Odoo invoice ref."""
        return f"WOO-{self.number}"

    def get_meta(self, key: str) -> str | None:
        """Return the value of a WooCommerce meta_data field by key, or None if absent."""
        for m in self.meta_data:
            if m.key == key:
                return m.value
        return None

    def _find_meta_value(self, keys: tuple[str, ...]) -> str:
        normalized_keys = {key.lower() for key in keys}
        for meta in self.meta_data:
            key = (meta.key or "").strip().lower()
            if key in normalized_keys:
                value = (meta.value or "").strip()
                if value:
                    return value
        return ""



# WOOCOMMERCE PRODUCT PAYLOADS (Product sync)
class WooCategory(BaseModel):
    """WooCommerce categorie — id is vereist voor correcte koppeling."""
    id: int | None = None
    name: str | None = None


class WooAttribute(BaseModel):
    """WooCommerce product attribuut, bijv. Merk: ['Hummel']."""
    name: str
    options: list[str]


class WooBrand(BaseModel):
    """WooCommerce product brand referentie voor payload."""
    id: int


class WooProductPayload(BaseModel):
    """WooCommerce product payload voor POST (aanmaken) en PUT (bijwerken)."""
    name: str
    sku: str
    regular_price: str
    status: str = "publish"
    sale_price: str | None = None
    short_description: str
    manage_stock: bool = True
    stock_quantity: int
    categories: list[WooCategory] = []
    attributes: list[WooAttribute] = []
    brands: list[WooBrand] = []
    global_unique_id: str | None = None
    tax_class: str | None = None
    meta_data: list[dict] = []


# INTERNAL STANDARD ORDER (ERP-agnostic)
class OrderLine(BaseModel):
    name: str           # product description shown on the invoice
    sku: str            # used to look up the product in Odoo
    quantity: int
    unit_price: float   # price per unit (excl. shipping)


class StandardOrder(BaseModel):
    """
    Normalised order that is independent of both WooCommerce and the target ERP.

    Adapters (OdooAdapter, SAPAdapter, …) receive this object and translate it
    into their own API calls.
    """

    external_id: str        # "WOO-123"  – used as dedup key in Odoo invoice ref
    woo_order_id: int
    is_paid: bool
    currency: str
    payment_method_title: str

    # Customer info
    customer_name: str
    customer_email: str
    customer_phone: str
    customer_company: str

    # Billing address
    street: str
    city: str
    zip_code: str
    country_code: str       # ISO-2, e.g. "BE"

    # Odoo references for linking
    odoo_s_number: str | None = None    # Sale order number, e.g. "S-00123"
    odoo_partner_id: int | None = None  # Customer/partner ID in Odoo

    lines: list[OrderLine]
    shipping_total: float   # Added as a separate invoice line when > 0
    total: float            # Grand total (incl. shipping, incl. tax)


# ODOO COMPANY CONFIGURATION (Per-company settings for WooCommerce sync)
class SyncIntervalType(str, Enum):
    """Available sync interval options for order imports."""
    INSTANT = "instant"        # Webhook-based immediate sync (future)
    EVERY_15_MIN = "15"        # Every 15 minutes (default)
    EVERY_30_MIN = "30"        # Every 30 minutes
    EVERY_1_HOUR = "60"        # Every hour
    EVERY_6_HOURS = "360"      # Every 6 hours
    EVERY_24_HOURS = "1440"    # Once per day
    MANUAL = "manual"          # Manual sync only


class SyncIntervalMode(str, Enum):
    """How order and product sync intervals should be scheduled."""
    SHARED = "shared"          # Orders and products share one interval
    SEPARATE = "separate"      # Orders and products each have their own interval


class CompanyWooSyncConfig(BaseModel):
    """
    WooCommerce sync configuration for an Odoo company.
    These settings are stored as company fields in Odoo (res.company).
    
    Field names map to Odoo field names like:
    - woo_sync_enabled
    - woo_sync_interval  
    - woo_auto_confirm_paid_orders
    """
    
    # Identification
    company_id: int             # Odoo company ID
    company_name: str           # Company name for display
    woo_wordpress_plugin_enabled: bool = False               # Master switch for WooCommerce/WordPress connector
    shopify_plugin_enabled: bool = False                      # Master switch for Shopify connector
    
    # Sync enablement & scheduling
    woo_sync_enabled: bool = False                             # Enable/disable sync for this company
    woo_sync_interval_mode: SyncIntervalMode = SyncIntervalMode.SEPARATE  # Shared or separate scheduling
    woo_sync_interval: SyncIntervalType = SyncIntervalType.EVERY_15_MIN  # How often to sync orders
    woo_product_sync_interval: SyncIntervalType | None = None   # How often to sync products when separate
    
    # Order handling
    woo_auto_confirm_paid_orders: bool = False                # Auto-confirm paid orders as sales
    woo_auto_confirm_unpaid_orders: bool = False              # Also confirm unpaid orders
    woo_use_stock_delivery_flow: bool = False                 # Enable stock/leverbon picking flow for paid orders
    woo_create_delivery_addresses: bool = False               # Create separate delivery addresses in Odoo when shipping differs from billing
    woo_track_stock: bool = False                             # Reserveer voorraad bij leverbon


    # WooCommerce connection (per company)
    woo_url: str | None = None                                # Company-specific WooCommerce URL (if different)
    woo_consumer_key: str | None = None                       # Company-specific API key (optional)
    woo_consumer_secret: str | None = None                    # Company-specific API secret (optional)

    # Runtime status (set from Odoo button/script feedback loop)
    woo_last_sync_status: str | None = None
    woo_last_error_message: str | None = None
    
    @property
    def sync_interval_minutes(self) -> int | None:
        """
        Get sync interval in minutes.
        Returns None for 'instant' or 'manual' modes.
        """
        if self.woo_sync_interval in (SyncIntervalType.INSTANT, SyncIntervalType.MANUAL):
            return None
        try:
            return int(self.woo_sync_interval.value)
        except (ValueError, AttributeError):
            return None

    @property
    def is_separate_sync_intervals(self) -> bool:
        """Check whether orders and products use separate schedules."""
        return self.woo_sync_interval_mode == SyncIntervalMode.SEPARATE

    @property
    def product_sync_interval_minutes(self) -> int | None:
        """Get the product sync interval in minutes, falling back to the order interval."""
        interval_source = (
            self.woo_product_sync_interval
            if self.is_separate_sync_intervals
            else self.woo_sync_interval
        )
        if interval_source in (SyncIntervalType.INSTANT, SyncIntervalType.MANUAL):
            return None
        try:
            return int(interval_source.value)
        except (ValueError, AttributeError):
            return self.sync_interval_minutes
    
    @property
    def is_manual_only(self) -> bool:
        """Check if sync is manual-only."""
        return self.woo_sync_interval == SyncIntervalType.MANUAL
    
    @property
    def is_instant(self) -> bool:
        """Check if sync is webhook-based (instant)."""
        return self.woo_sync_interval == SyncIntervalType.INSTANT


# ODOO API RESPONSE MODELS (For fetching company configs)
class OdooCompanyResponse(BaseModel):
    """
    Response from Odoo API when fetching company data.
    Maps directly to res.company fields.
    """
    id: int
    name: str
    
    # WooCommerce sync config fields (optional presence)
    woo_wordpress_plugin_enabled: bool | None = None
    shopify_plugin_enabled: bool | None = None
    woo_sync_enabled: bool | None = None
    woo_sync_interval_mode: str | None = None
    woo_sync_interval: str | None = None
    woo_product_sync_interval: str | None = None
    woo_auto_confirm_paid_orders: bool | None = None
    woo_auto_confirm_unpaid_orders: bool | None = None
    woo_track_stock: bool | None = None
    woo_create_delivery_addresses: bool | None = None
    woo_url: str | None = None
    woo_consumer_key: str | None = None
    woo_consumer_secret: str | None = None
    woo_last_sync_status: str | None = None
    woo_last_error_message: str | None = None

    @field_validator(
        "woo_sync_interval_mode",
        "woo_sync_interval",
        "woo_product_sync_interval",
        "woo_url",
        "woo_consumer_key",
        "woo_consumer_secret",
        "woo_last_sync_status",
        "woo_last_error_message",
        mode="before",
    )
    @classmethod
    def false_to_none_for_strings(cls, v: Any) -> Any:
        """Odoo may return False for empty optional Char/Text fields."""
        return None if v is False else v
    
    def to_config(self) -> CompanyWooSyncConfig:
        """Convert Odoo response to config model with defaults."""
        return CompanyWooSyncConfig(
            company_id=self.id,
            company_name=self.name,
            woo_wordpress_plugin_enabled=(
                self.woo_wordpress_plugin_enabled
                if self.woo_wordpress_plugin_enabled is not None
                else False
            ),
            shopify_plugin_enabled=(
                self.shopify_plugin_enabled
                if self.shopify_plugin_enabled is not None
                else False
            ),
            woo_sync_enabled=self.woo_sync_enabled if self.woo_sync_enabled is not None else False,
            woo_sync_interval_mode=(
                SyncIntervalMode(self.woo_sync_interval_mode)
                if self.woo_sync_interval_mode
                else SyncIntervalMode.SEPARATE
            ),
            woo_sync_interval=SyncIntervalType(self.woo_sync_interval) 
                if self.woo_sync_interval 
                else SyncIntervalType.EVERY_15_MIN,
            woo_product_sync_interval=(
                SyncIntervalType(self.woo_product_sync_interval)
                if self.woo_product_sync_interval
                else None
            ),
            woo_auto_confirm_paid_orders=(
                self.woo_auto_confirm_paid_orders
                if self.woo_auto_confirm_paid_orders is not None
                else False
            ),
            woo_auto_confirm_unpaid_orders=(
                self.woo_auto_confirm_unpaid_orders
                if self.woo_auto_confirm_unpaid_orders is not None
                else False
            ),
            woo_track_stock=(
                self.woo_track_stock
                if self.woo_track_stock is not None
                else False
            ),
            woo_create_delivery_addresses=(
                self.woo_create_delivery_addresses
                if self.woo_create_delivery_addresses is not None
                else False
            ),

            woo_url=self.woo_url,
            woo_consumer_key=self.woo_consumer_key,
            woo_consumer_secret=self.woo_consumer_secret,
            woo_last_sync_status=self.woo_last_sync_status,
            woo_last_error_message=self.woo_last_error_message,
        )


# CUSTOMER VERIFICATION & CLASSIFICATION
class CustomerType(str, Enum):
    """Customer classification: Business (B2B) or Consumer (B2C)."""
    B2B = "b2b"         # Business to Business (has company name, VAT number, etc.)
    B2C = "b2c"         # Business to Consumer (individual person)
    UNKNOWN = "unknown"  # Classification inconclusive


class CustomerClassification(BaseModel):
    """
    Classification result for a customer order.
    Determines if it's B2C (individual) or B2B (business).
    """
    customer_type: CustomerType = CustomerType.UNKNOWN
    confidence: float = 0.0  # 0.0 to 1.0 confidence score
    
    # Evidence for classification
    has_company_name: bool = False
    has_vat_number: bool = False
    email_is_corporate: bool = False
    name_looks_person: bool = False
    
    # Reasoning
    reasoning: str = ""
    
    @property
    def is_b2b(self) -> bool:
        """Check if classified as B2B."""
        return self.customer_type == CustomerType.B2B
    
    @property
    def is_b2c(self) -> bool:
        """Check if classified as B2C."""
        return self.customer_type == CustomerType.B2C
    
    @property
    def confidence_percentage(self) -> int:
        """Get confidence as percentage."""
        return int(self.confidence * 100)


class AddressComparison(BaseModel):
    """Comparison of billing vs shipping address."""
    billing_street: str
    billing_city: str
    billing_zip: str
    
    shipping_street: str | None = None
    shipping_city: str | None = None
    shipping_zip: str | None = None
    
    addresses_identical: bool = False
    is_different_delivery: bool = False
    
    @classmethod
    def from_woo_order(cls, billing: "WooBillingAddress", shipping: "WooBillingAddress" | None = None) -> "AddressComparison":
        """Create comparison from WooCommerce order billing/shipping."""
        billing_key = cls._address_key(billing.address_1, billing.city, billing.postcode)
        
        if shipping:
            shipping_key = cls._address_key(shipping.address_1, shipping.city, shipping.postcode)
            identical = billing_key == shipping_key
        else:
            shipping_key = None
            identical = True
        
        result = cls(
            billing_street=billing.address_1,
            billing_city=billing.city,
            billing_zip=billing.postcode,
            shipping_street=shipping.address_1 if shipping else None,
            shipping_city=shipping.city if shipping else None,
            shipping_zip=shipping.postcode if shipping else None,
            addresses_identical=identical,
            is_different_delivery=not identical and shipping is not None,
        )
        
        return result

    @staticmethod
    def _address_key(street: str | None, city: str | None, postcode: str | None) -> str:
        parts = [street or "", city or "", postcode or ""]
        return "|".join(re.sub(r"\s+", " ", part).strip().lower() for part in parts)


class CustomerMatch(BaseModel):
    """A potential match between WooCommerce customer and Odoo partner."""
    odoo_partner_id: int
    odoo_partner_name: str
    match_type: str  # "email", "phone", "name_address", "fuzzy"
    confidence: float  # 0.0 to 1.0
    
    # Why this match was suggested
    matched_on: dict  # {"email": True, "phone": False, ...}
    
    @property
    def confidence_percentage(self) -> int:
        """Get confidence as percentage."""
        return int(self.confidence * 100)


class CustomerVerificationResult(BaseModel):
    """Complete customer verification result for a WooCommerce order."""
    
    # WooCommerce customer info
    woo_customer_id: int
    woo_email: str
    woo_customer_name: str
    woo_company_name: str | None = None
    
    # Classification
    classification: CustomerClassification = CustomerClassification()
    
    # Address handling
    address_comparison: AddressComparison
    
    # Matching
    exact_match_found: bool = False
    exact_match: CustomerMatch | None = None
    
    potential_matches: list[CustomerMatch] = []  # Alternative suggestions
    
    # Verification status
    verification_status: str  # "auto_matched", "manual_review_needed", "new_customer"
    issue_description: str = ""  # Why verification is needed (if any)
    
    # Recommendation for Odoo
    recommended_action: str  # "create_new", "link_existing", "manual_review"
    recommended_partner_id: int | None = None


class CustomerVerificationReport(BaseModel):
    """Report of customer verification for batch processing."""
    total_customers: int
    auto_matched: int  # Exact matches found
    manual_review_needed: int  # Needs human verification
    new_customers: int  # No matches, create new
    
    unmatched_customers: list[CustomerVerificationResult] = []
    
    @property
    def unmatched_percentage(self) -> float:
        """Percentage of customers needing attention."""
        unmatched = self.manual_review_needed + self.new_customers
        if self.total_customers == 0:
            return 0.0
        return (unmatched / self.total_customers) * 100

