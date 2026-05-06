"""
woo_controller.py
─────────────────
Unified WooCommerce REST API (v3) controller.
Handles both order imports and product synchronization.

Authenticatie: HTTP Basic Auth via consumer_key:consumer_secret.
"""

import logging
from typing import Any
import httpx
import sys
from pathlib import Path

# Add parent folder to path for explicit imports
sys.path.append(str(Path(__file__).parent.parent))

from config import settings
from shared.models import WooOrder, WooProductStock
from shared.odoo_controller import OdooController

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_WOO_API_BASE = "/wp-json/wc/v3"
_PAGE_SIZE = 100


class WooController:
    """
    Unified WooCommerce API wrapper for orders and products synchronization.
    
    Gebruik:
        woo = WooController()
        orders = woo.get_orders(status="processing")
        products = woo.get_products_stock()
        sku_map = woo.get_all_skus()
    """

    def __init__(
        self,
        woo_url: str | None = None,
        woo_consumer_key: str | None = None,
        woo_consumer_secret: str | None = None,
    ) -> None:
        resolved_url = (woo_url or settings.woo_url or "").strip()
        resolved_key = (woo_consumer_key or settings.woo_consumer_key or "").strip()
        resolved_secret = (woo_consumer_secret or settings.woo_consumer_secret or "").strip()

        if not resolved_url:
            raise ValueError(
                "WooCommerce URL ontbreekt. Vul woo_url in Odoo company settings in of WOO_URL in .env."
            )
        if not (resolved_url.startswith("http://") or resolved_url.startswith("https://")):
            raise ValueError(
                f"WooCommerce URL moet met http:// of https:// starten, ontvangen: '{resolved_url}'"
            )
        if not resolved_key:
            raise ValueError(
                "WooCommerce Consumer Key ontbreekt. Vul woo_consumer_key in Odoo company settings in of WOO_CONSUMER_KEY in .env."
            )
        if not resolved_secret:
            raise ValueError(
                "WooCommerce Consumer Secret ontbreekt. Vul woo_consumer_secret in Odoo company settings in of WOO_CONSUMER_SECRET in .env."
            )

        self.base_url = resolved_url.rstrip("/") + _WOO_API_BASE
        self.auth = (resolved_key, resolved_secret)
        self._client = httpx.Client(timeout=30, auth=self.auth)

    # ══════════════════════════════════════════════════════════════════════════
    # CORE: API HELPERS
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _normalize_path(path: str) -> str:
        """Accept both 'products' and '/products' and normalize to '/products'."""
        if not path:
            return "/"
        return path if path.startswith("/") else f"/{path}"

    def _get(self, path: str, params: dict | None = None) -> Any:
        """GET request to WooCommerce API."""
        url = f"{self.base_url}{self._normalize_path(path)}"
        response = self._client.get(url, params=params or {})
        response.raise_for_status()
        return response.json()

    def _post(self, path: str, payload: dict) -> dict:
        """POST request to WooCommerce API."""
        url = f"{self.base_url}{self._normalize_path(path)}"
        response = self._client.post(url, json=payload)
        response.raise_for_status()
        return response.json()

    def _put(self, path: str, payload: dict) -> dict:
        """PUT request to WooCommerce API."""
        url = f"{self.base_url}{self._normalize_path(path)}"
        response = self._client.put(url, json=payload)
        response.raise_for_status()
        return response.json()

    def _delete(self, path: str, params: dict | None = None) -> dict:
        """DELETE request to WooCommerce API."""
        url = f"{self.base_url}{self._normalize_path(path)}"
        response = self._client.delete(url, params=params or {})
        response.raise_for_status()
        return response.json()

    # ══════════════════════════════════════════════════════════════════════════
    # ORDER SYNC: FETCH & RETRIEVE
    # ══════════════════════════════════════════════════════════════════════════

    def get_orders(
        self,
        status: str | None = None,
        after: str | None = None,
        before: str | None = None,
        include: list[int] | None = None,
    ) -> list[WooOrder]:
        """
        Haal orders op, automatisch gepagineerd.

        Args:
            status:  WooCommerce statusfilter, bijv. "processing", "completed", "any".
            after:   ISO-8601 datum – alleen orders aangemaakt NA dit tijdstip.
            before:  ISO-8601 datum – alleen orders aangemaakt VOOR dit tijdstip.
            include: Lijst van WooCommerce order-IDs om te filteren.

        Returns list van WooOrder objecten sorted oudste eerst.
        """
        orders: list[WooOrder] = []
        page = 1
        params: dict[str, Any] = {
            "per_page": _PAGE_SIZE,
            "orderby": "date",
            "order": "asc",
        }

        if status:
            params["status"] = status
        if after:
            params["after"] = after
        if before:
            params["before"] = before
        if include:
            params["include"] = ",".join(str(i) for i in include)

        while True:
            params["page"] = page
            batch: list[dict] = self._get("/orders", params=params)

            if not batch:
                break

            for raw in batch:
                try:
                    orders.append(WooOrder.model_validate(raw))
                except Exception as exc:
                    logger.warning(
                        "Order id=%s overgeslagen – parse fout: %s",
                        raw.get("id"),
                        exc,
                    )

            if len(batch) < _PAGE_SIZE:
                break

            page += 1

        return orders

    def update_order_meta(self, order_id: int, meta_key: str, meta_value: str) -> bool:
        """
        Update or add a meta field to a WooCommerce order.
        
        Args:
            order_id: WooCommerce order ID
            meta_key: Meta field key, e.g. "_odoo_s_number"
            meta_value: Value to store
        
        Returns:
            True if successful, False otherwise
        """
        try:
            payload = {
                "meta_data": [
                    {"key": meta_key, "value": meta_value}
                ]
            }
            self._put(f"/orders/{order_id}", payload)
            logger.info(
                "Order #%s: meta field '%s' = '%s'",
                order_id,
                meta_key,
                meta_value
            )
            return True
        except Exception as exc:
            logger.error(
                "Error updating order #%s meta field '%s': %s",
                order_id,
                meta_key,
                exc
            )
            return False

    def add_order_note(
        self,
        order_id: int,
        note: str,
        customer_note: bool = False,
    ) -> bool:
        """
        Add an order note in WooCommerce (Order Notes timeline).

        Args:
            order_id: WooCommerce order ID
            note: Note text to add
            customer_note: If True, visible to customer as note notification

        Returns:
            True if successful, False otherwise
        """
        try:
            payload = {
                "note": note,
                "customer_note": customer_note,
            }
            self._post(f"/orders/{order_id}/notes", payload)
            logger.info("Order #%s: note toegevoegd: %s", order_id, note)
            return True
        except Exception as exc:
            logger.error("Error adding note to order #%s: %s", order_id, exc)
            return False

    def update_order_sync_status(
        self,
        order_id: int,
        s_number: str | None = None,
        status: str = "synced",
        error_message: str | None = None,
        partner_id: int | None = None,
    ) -> bool:
        """
        Update comprehensive sync status metadata for a WooCommerce order.
        
        This writes all sync-related information as order meta fields for display
        in WooCommerce admin (via REST API or custom plugin extensions).
        
        Args:
            order_id: WooCommerce order ID
            s_number: Odoo sale order number (e.g., "S-000456")
            status: Sync status - "synced", "failed", "retry", "pending"
            error_message: Error description if status is "failed" or "retry"
            partner_id: Odoo partner (customer) ID
        
        Returns:
            True if successful, False otherwise
        """
        from datetime import datetime
        
        try:
            timestamp = datetime.now().isoformat()
            
            meta_updates = [
                {"key": "_odoo_sync_status", "value": status},
                {"key": "_odoo_sync_timestamp", "value": timestamp},
            ]

            if s_number:
                meta_updates.append(
                    {"key": "_odoo_s_number", "value": s_number}
                )
            
            if partner_id:
                meta_updates.append(
                    {"key": "_odoo_partner_id", "value": str(partner_id)}
                )
            
            if error_message:
                meta_updates.append(
                    {"key": "_odoo_sync_error", "value": error_message}
                )
            
            payload = {"meta_data": meta_updates}
            self._put(f"/orders/{order_id}", payload)
            
            logger.info(
                "Order #%s: sync status updated – status=%s, s_number=%s",
                order_id,
                status,
                s_number or "N/A",
            )
            return True
        except Exception as exc:
            logger.error(
                "Error updating order #%s sync status: %s",
                order_id,
                exc,
            )
            return False

    def get_order_sync_status(self, order_id: int) -> dict | None:
        """
        Retrieve comprehensive sync status metadata for a WooCommerce order.
        
        Useful for WooCommerce admin displays or status checks.
        
        Returns:
            Dict with keys: status, timestamp, s_number, partner_id, error_message
            or None if order not found or meta fields not set.
        """
        try:
            orders = self._get(f"/orders/{order_id}")
            
            if not orders:
                return None
            
            order = orders if isinstance(orders, dict) else orders[0]
            meta_data = order.get("meta_data", [])
            
            # Extract sync-related meta fields
            sync_info = {
                "status": None,
                "timestamp": None,
                "s_number": None,
                "partner_id": None,
                "error_message": None,
            }
            
            for meta in meta_data:
                key = meta.get("key", "")
                value = meta.get("value", "")
                
                if key == "_odoo_sync_status":
                    sync_info["status"] = value
                elif key == "_odoo_sync_timestamp":
                    sync_info["timestamp"] = value
                elif key == "_odoo_partner_id":
                    sync_info["partner_id"] = value
                elif key == "_odoo_sync_error":
                    sync_info["error_message"] = value
            
            return sync_info if any(sync_info.values()) else None
        except Exception as exc:
            logger.error(
                "Error retrieving sync status for order #%s: %s",
                order_id,
                exc,
            )
            return None

    # ══════════════════════════════════════════════════════════════════════════
    # PRODUCT SYNC: FETCH PRODUCTS & STOCK
    # ══════════════════════════════════════════════════════════════════════════

    def get_products_stock(self) -> list[WooProductStock]:
        """
        Haal WooCommerce producten op met velden nodig voor stocksync.
        """
        products: list[WooProductStock] = []
        page = 1
        params: dict[str, Any] = {
            "per_page": _PAGE_SIZE,
            "orderby": "id",
            "order": "asc",
            "_fields": "id,name,sku,stock_quantity",
        }

        while True:
            params["page"] = page
            batch: list[dict] = self._get("/products", params=params)

            if not batch:
                break

            for raw in batch:
                try:
                    products.append(WooProductStock.model_validate(raw))
                except Exception as exc:
                    logger.warning(
                        "Product id=%s overgeslagen – parse fout: %s",
                        raw.get("id"),
                        exc,
                    )

            if len(batch) < _PAGE_SIZE:
                break

            page += 1

        return products

    # ══════════════════════════════════════════════════════════════════════════
    # PRODUCT SYNC METHODS – Create/Update
    # ══════════════════════════════════════════════════════════════════════════

    def get_all_skus(self) -> dict[str, int]:
        """
        Pagineer door alle WooCommerce producten en geef mapping terug:
        SKU → WooCommerce product ID.

        Voorbeeld: { "SKU-001": 42, "WIDGET-RED": 99 }
        """
        sku_map: dict[str, int] = {}
        page = 1

        while True:
            logger.info("WooCommerce SKU's ophalen – pagina=%s", page)

            products: list[dict] = self._get(
                "/products",
                params={
                    "per_page": _PAGE_SIZE,
                    "page": page,
                    "status": "any",
                    "_fields": "id,sku",
                },
            )

            if not products:
                break

            for product in products:
                sku = product.get("sku", "").strip()
                if sku:
                    sku_map[sku] = product["id"]

            if len(products) < _PAGE_SIZE:
                break

            page += 1

        logger.info("WooCommerce SKU-map gebouwd – %s SKU's gevonden", len(sku_map))
        return sku_map

    def get_all_brands(self) -> dict[str, int]:
        """Haal alle WooCommerce merken op. Geeft dict terug: naam (lowercase) -> ID."""
        brand_map: dict[str, int] = {}
        page = 1
        while True:
            brands: list[dict] = self._get(
                "/products/brands",
                params={"per_page": 100, "page": page, "_fields": "id,name"},
            )
            if not brands:
                break
            for brand in brands:
                if brand.get("name"):
                    brand_map[str(brand["name"]).strip().lower()] = brand["id"]
            if len(brands) < 100:
                break
            page += 1
        logger.info("WooCommerce merken-map gebouwd – %s merken", len(brand_map))
        return brand_map

    def get_or_create_brand(self, name: str, brand_map: dict[str, int]) -> int | None:
        """Geeft WooCommerce brand ID terug. Maakt merk aan indien het niet bestaat."""
        normalized = name.strip().lower()
        if not normalized:
            return None
        if normalized in brand_map:
            return brand_map[normalized]

        logger.info("Merk aanmaken in WooCommerce: %s", name)
        try:
            result = self._post("/products/brands", {"name": name})
            woo_id: int = result["id"]
            brand_map[normalized] = woo_id
            logger.info("Merk aangemaakt: %s (id=%s)", name, woo_id)
            return woo_id
        except Exception as exc:
            logger.warning("Kon merk '%s' niet aanmaken: %s", name, exc)
            return None

    def create_product(self, payload: dict) -> dict:
        """POST /products – maak een nieuw product aan in WooCommerce."""
        logger.info("WooCommerce product aanmaken – SKU=%s", payload.get("sku"))
        return self._post("/products", payload)

    def update_product(self, woo_id: int, payload: dict) -> dict:
        """PUT /products/{id} – werk een product bij in WooCommerce."""
        logger.info("WooCommerce product bijwerken – woo_id=%s SKU=%s", woo_id, payload.get("sku"))
        return self._put(f"/products/{woo_id}", payload)

    def trash_product(self, woo_id: int) -> dict:
        """
        Verplaats product naar prullenbak in WooCommerce.

        force=false betekent soft-delete (naar trash), waardoor het product niet meer zichtbaar is op de storefront.
        """
        logger.info("WooCommerce product naar prullenbak – woo_id=%s", woo_id)
        return self._delete(f"/products/{woo_id}", params={"force": "false"})

    def hard_delete_product(self, woo_id: int) -> dict:
        """
        Verwijder product permanent uit WooCommerce.

        Let op: force=true is onomkeerbaar en wordt alleen gebruikt in expliciete hard-delete modus.
        """
        logger.info("WooCommerce product hard verwijderen – woo_id=%s", woo_id)
        return self._delete(f"/products/{woo_id}", params={"force": "true"})

    def get_customer(self, customer_id: int) -> dict | None:
        """
        Haal volledige klantgegevens op van WooCommerce, inclusief VAT uit meta_data.

        Returns:
            Dict met volledige klantgegevens (id, email, first_name, last_name, billing,
            company_name, vat_number uit meta_data).
        """
        if not customer_id or customer_id == 0:
            logger.debug("get_customer: Gastbeseller (ID=%s) – geen klantgegevens beschikbaar", customer_id)
            return None

        try:
            logger.debug("WooCommerce klantgegevens ophalen – customer_id=%s", customer_id)
            customer = self._get(f"/customers/{customer_id}") #Endpoint

            # Extract VAT from meta_data
            vat_number = self._extract_meta_value(
                customer.get("meta_data", []),
                ["billing_eu_vat_number", "_vat_number", "_billing_vat"]
            )

            # Extract company from meta_data or billing object
            company_name = customer.get("billing", {}).get("company", "")
            if not company_name:
                company_name = self._extract_meta_value(
                    customer.get("meta_data", []),
                    ["billing_company", "_billing_company", "company_name"]
                )

            # Enrich the customer dict with extracted data
            customer["vat_number"] = vat_number
            customer["company_name"] = company_name

            logger.debug(
                "Klantgegevens opgehaald – customer_id=%s email=%s company=%s vat=%s",
                customer_id,
                customer.get("email", ""),
                company_name or "(geen)",
                vat_number or "(geen)",
            )

            return customer

        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.debug("Klant niet gevonden – customer_id=%s (404)", customer_id)
                return None
            logger.error(
                "WooCommerce API-fout bij ophalen klant %s: HTTP %s – %s",
                customer_id,
                exc.response.status_code,
                exc.response.text[:500],
            )
            raise
        except Exception as exc:
            logger.error("Onverwachte fout bij ophalen klant %s: %s", customer_id, exc)
            raise

    @staticmethod
    def _extract_meta_value(meta_data: list[dict], keys: list[str]) -> str:
        """
        Extract value from WooCommerce meta_data array by searching for matching keys.

        meta_data format:
            [
              {"id": 123, "key": "billing_eu_vat_number", "value": "BE 0123.456.789"},
              {"id": 124, "key": "other_key", "value": "..."}
            ]

        Returns:
            Value if found, empty string otherwise
        """
        if not meta_data:
            return ""

        for key in keys:
            for meta_item in meta_data:
                if meta_item.get("key") == key:
                    value = meta_item.get("value", "").strip()
                    if value:
                        return value

        return ""