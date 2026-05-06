"""
mapper.py
─────────
Mapping logic for WooCommerce synchronization:
  - Order import: WooOrder → Odoo sale.order
  - Product sync: OdooProduct → WooCommerce product payload
"""

import logging
from datetime import datetime

from shared.models import (
    OrderLine, StandardOrder, WooLineItem, WooOrder,
    OdooProduct, WooBrand, WooProductPayload
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# ORDER IMPORT: WooCommerce → Odoo
# ══════════════════════════════════════════════════════════════════════════════

def woo_order_to_standard(order: WooOrder) -> StandardOrder:
    """
    Convert WooCommerce order to ERP-agnostic StandardOrder format.

    - Shipping costs stored separately for invoice line creation
    - Unit price = total / qty to handle discounts correctly
    """
    billing = order.billing

    customer_name = f"{billing.first_name} {billing.last_name}".strip()
    if not customer_name:
        customer_name = billing.company or f"Customer #{order.id}"

    lines: list[OrderLine] = []
    for item in order.line_items:
        qty = max(item.quantity, 1)
        try:
            unit_price = round(float(item.total) / qty, 6)
        except (ValueError, ZeroDivisionError):
            unit_price = float(item.price or 0)

        lines.append(
            OrderLine(
                name=item.name,
                sku=item.sku,
                quantity=qty,
                unit_price=unit_price,
            )
        )

    shipping_total = 0.0
    for shipping_line in order.shipping_lines:
        try:
            shipping_total += float(shipping_line.total)
        except ValueError:
            pass

    return StandardOrder(
        external_id=order.reference,
        woo_order_id=order.id,
        is_paid=order.is_paid,
        currency=order.currency,
        payment_method_title=order.payment_method_title,
        customer_name=customer_name,
        customer_email=billing.email,
        customer_phone=billing.phone,
        customer_company=billing.company,
        street=(
            f"{billing.address_1} {billing.address_2}".strip()
            if billing.address_2
            else billing.address_1
        ),
        city=billing.city,
        zip_code=billing.postcode,
        country_code=billing.country,
        lines=lines,
        shipping_total=round(shipping_total, 2),
        total=round(float(order.total), 2),
    )


STATUS_MAP: dict[str, str] = {
    "pending":    "draft",
    "processing": "sale",
    "on-hold":    "draft",
    "completed":  "done",
    "cancelled":  "cancel",
    "refunded":   "cancel",
    "failed":     "cancel",
}


class OrderMapper:
    """
    Maps WooOrder to Odoo sale.order vals dict.

    Raises ValueError if:
      - Order line missing SKU
      - SKU not found in product_map

    Usage:
        vals     = OrderMapper.map(order, partner_id, currency_id, product_map)
        order_id = odoo.create_sale_order(vals)
    """

    @staticmethod
    def map(
        order: WooOrder,
        partner_id: int,
        currency_id: int,
        product_map: dict[str, int],  # SKU → product.product id
        shipping_partner_id: int | None = None,
    ) -> tuple[dict, list[dict]]:
        """
        Create sale.order vals dict and separate line items for Odoo (JSON-2 compatible).

        Args:
            order:       WooCommerce order object
            partner_id:  Odoo res.partner id
            currency_id: Odoo res.currency id (e.g., EUR)
            product_map: Dict {sku: product_id}
            shipping_partner_id: Optional Odoo res.partner id for delivery address

        Returns:
            tuple of (order_vals, order_lines)
              - order_vals: dict with sale.order header fields (NO nested order_line)
              - order_lines: list of dicts for separate line creation via JSON-2

        Raises:
            ValueError: If product not found
        """
        try:
            date_order = (
                datetime.fromisoformat(order.date_created)
                .strftime("%Y-%m-%d %H:%M:%S")
            )
        except (ValueError, TypeError):
            date_order = order.date_created

        order_lines = OrderMapper._map_line_items(order.line_items, product_map)

        order_vals = {
            "partner_id": partner_id,
            "partner_shipping_id": shipping_partner_id or partner_id,
            "currency_id": currency_id,
            "date_order": date_order,
            "note": f"WooCommerce Order #{order.number}",
            "client_order_ref": order.number,
        }

        return order_vals, order_lines

    @staticmethod
    def _map_line_items(
        line_items: list[WooLineItem],
        product_map: dict[str, int],
    ) -> list[dict]:
        """
        Translate WooCommerce order lines to Odoo sale.order.line vals (JSON-2 format).
        
        Note:
        - Removes ORM-style tax commands; WooCommerce prices are pre-calculated
        - Keeps line description in 'name' (required by sale.order.line)
        - All fields compatible with JSON-2 REST API (no ORM commands)
        """
        lines: list[dict] = []

        for item in line_items:
            sku = (item.sku or "").strip()

            if not sku and item.product_id == 0:
                raise ValueError(
                    f"Order line '{item.name}' missing SKU and product_id=0. "
                    "Import product via product sync first."
                )

            if not sku:
                raise ValueError(
                    f"Order line '{item.name}' missing SKU. "
                    "Cannot lookup product in Odoo."
                )

            product_id = product_map.get(sku)
            if product_id is None:
                raise ValueError(
                    f"Product with SKU '{sku}' not found in Odoo. "
                    "Import via product sync first."
                )

            qty = max(item.quantity, 1)

            try:
                price_unit = round(float(item.total) / qty, 6)
            except (ValueError, ZeroDivisionError):
                price_unit = float(item.price or 0)

            # JSON-2 compatible line dict (no ORM commands).
            # 'name' is required on sale.order.line for JSON-2 create.
            line: dict = {
                "product_id": product_id,
                "name": item.name or sku,
                "product_uom_qty": qty,
                "price_unit": price_unit,
            }

            lines.append(line)

        return lines


# ══════════════════════════════════════════════════════════════════════════════
# PRODUCT SYNC: Odoo → WooCommerce
# ══════════════════════════════════════════════════════════════════════════════

def _extract_brand_name_from_category(categ_id: list | None) -> str | None:
    """Extract brand name from Odoo categ_id ([id, 'Name'])."""
    if not categ_id or not isinstance(categ_id, (list, tuple)) or len(categ_id) < 2:
        return None

    name = str(categ_id[1]).replace("All / ", "").strip()
    if not name:
        return None
    return name


def map_odoo_to_woo(product: OdooProduct, brand_map: dict[str, int] | None = None) -> WooProductPayload:
    """
    Convert OdooProduct to WooCommerce product payload.
    
    Categories and attributes skipped to avoid server issues.
    """
    # Categories and attributes left empty
    categories = []
    attributes = []

    sku = (product.default_code or "").strip()
    if not sku:
        sku = f"ODOO-{product.template_id or product.id}"

    brands: list[WooBrand] = []
    brand_name = _extract_brand_name_from_category(product.categ_id)
    if brand_name and brand_map:
        woo_brand_id = brand_map.get(brand_name.strip().lower())
        if woo_brand_id is not None:
            brands = [WooBrand(id=woo_brand_id)]
        else:
            logger.warning("Brand '%s' not found in WooCommerce map, skipping.", brand_name)

    # EAN/GTIN
    global_unique_id: str | None = None

    # Meta data: compatibility for installs reading custom ean meta
    meta: list[dict] = []
    if getattr(product, "barcode", None):
        ean = str(product.barcode)
        global_unique_id = ean
        meta.append({"key": "ean", "value": ean})

    regular_price = round(product.list_price, 2)

    sale_price_value: float | None = None
    if product.sale_price is not None:
        try:
            parsed_sale = round(float(product.sale_price), 2)
            if 0 < parsed_sale < regular_price:
                sale_price_value = parsed_sale
        except (TypeError, ValueError):
            sale_price_value = None
    elif product.discount_percent is not None:
        try:
            discount = float(product.discount_percent)
            if 0 < discount < 100:
                discounted_price = round(regular_price * (1 - discount / 100), 2)
                if 0 < discounted_price < regular_price:
                    sale_price_value = discounted_price
        except (TypeError, ValueError):
            sale_price_value = None

    # Odoo is de master: leeg sale_price wist een bestaande actieprijs in WooCommerce.
    sale_price = str(sale_price_value) if sale_price_value is not None else ""

    return WooProductPayload(
        name=product.name,
        sku=sku,
        regular_price=str(regular_price),
        status="publish",
        sale_price=sale_price,
        short_description=product.resolved_description,
        manage_stock=False,
        stock_quantity=None,
        categories=categories,
        attributes=attributes,
        brands=brands,
        global_unique_id=global_unique_id,
        tax_class=None,
        meta_data=meta,
    )
