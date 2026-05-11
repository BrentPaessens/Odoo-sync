"""
odoo_controller.py
──────────────────
Alle communicatie met de Odoo API voor verkooporders, facturen, betalingen en voorraad.

Ondersteunde versies (automatisch gedetecteerd):
  v14 – v18  → JSON-RPC 2.0          – auth via ODOO_USERNAME + ODOO_PASSWORD
  v19+       → REST API (JSON-2)     – auth via ODOO_API_KEY (Bearer token)

Methoden:
    1. authenticate()                – versiedetectie + authenticatie
    2. find_or_create_customer()     – res.partner opzoeken of aanmaken
    3. order_exists()                – dedup: WooCommerce order al aanwezig?
    4. find_product_by_sku()         – product.product opzoeken via SKU
    5. get_currency_id()             – res.currency id ophalen
    6. create_sale_order()           – sale.order / offerte aanmaken
    7. confirm_order()               – order bevestigen (action_confirm)
    8. lock_order()                  – order vergrendelen (action_lock)
    9. cancel_order()                – order annuleren (action_cancel)
    10. create_invoice_from_order()  – factuur aanmaken via wizard
    11. post_invoice()               – factuur posten (action_post)
    12. register_payment()           – betaling registreren
    13. create_credit_note()         – credit nota aanmaken
"""

import logging
import re
from datetime import datetime
from typing import Any
import httpx
import sys
from pathlib import Path

# Voeg parent directory toe voor imports
sys.path.insert(0, str(Path(__file__).parent))

from config import settings
from .models import WooBillingAddress

logger = logging.getLogger(__name__)

_MIN_VERSION = 14           # Min. Odoo versie 14
_REST_API_VERSION = 19      # Vanaf Odoo versie 19


class OdooController:
    """
    Detecteert automatisch de Odoo versie en kiest het juiste auth-pad:
      v14 – v18  → JSON-RPC 2.0   (ODOO_USERNAME + ODOO_PASSWORD)
      v19+       → JSON-2         (ODOO_API_KEY)
    """

    def __init__(self) -> None:
        self.url = settings.odoo_url.rstrip("/")
        self.db = settings.odoo_db
        self.username = settings.odoo_username
        self.password = settings.odoo_password
        self.api_key = settings.odoo_api_key
        self.uid: int | None = None
        self.odoo_version: int | None = None
        self._use_json2: bool = False
        self._call_id = 0
        # Persistent client – houdt sessie-cookies levend over alle requests
        self._client = httpx.Client(timeout=30)

    # PRIVATE HELPERS – JSON-RPC 2.0 (v14–v18)
    def _next_id(self) -> int:
        self._call_id += 1
        return self._call_id

    def _post(self, endpoint: str, params: dict) -> Any:
        """JSON-RPC 2.0 POST"""
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "id": self._next_id(),
            "params": params,
        }
        response = self._client.post(f"{self.url}{endpoint}", json=payload)
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise RuntimeError(
                f"Odoo RPC error: {data['error'].get('data', {}).get('message', data['error'])}"
            )
        return data.get("result")

    def _call_kw(
        self,
        model: str,
        method: str,
        args: list,
        kwargs: dict | None = None,
    ) -> Any:
        """Shortcut voor /web/dataset/call_kw."""
        if self.uid is None:
            raise RuntimeError("Roep authenticate() eerst aan.")
        return self._post(
            "/web/dataset/call_kw",
            {"model": model, "method": method, "args": args, "kwargs": kwargs or {}},
        )

    # PRIVATE HELPERS – JSON-2.0 (v19+)
    def _json2_search_read(
        self,
        model: str,
        domain: list,
        fields: list,
        limit: int = 0,
        offset: int = 0,
        suppress_not_found: bool = False,
    ) -> list[dict]:
        body: dict = {"domain": domain, "fields": fields}
        if limit:
            body["limit"] = limit
        if offset:
            body["offset"] = offset
        
        logger.debug(f"JSON-2 search_read request: {model}, body={body}")
        
        response = self._client.post(
            f"{self.url}/json/2/{model}/search_read",
            json=body,
        )
        
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if suppress_not_found and e.response.status_code == 404:
                logger.info(
                    "JSON-2 model '%s' niet beschikbaar. Stap wordt overgeslagen.",
                    model,
                )
                return []
            logger.error(f"JSON-2 API error: {e.response.status_code} – {e.response.text}")
            raise
        
        data = response.json()
        return data.get("records", data) if isinstance(data, dict) else data

    def _json2_search(self, model: str, domain: list) -> list[int]:
        records = self._json2_search_read(model, domain, ["id"])
        return [r["id"] for r in records]

    def _json2_create(self, model: str, vals: dict, context: dict | None = None) -> int:
        body: dict = {"vals_list": [vals]}
        if context:
            body["context"] = context
        response = self._client.post(f"{self.url}/json/2/{model}/create", json=body)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "JSON-2 create error model=%s status=%s body=%s response=%s",
                model,
                exc.response.status_code,
                body,
                exc.response.text,
            )
            raise
        data = response.json()
        if isinstance(data, list):
            return data[0]
        if isinstance(data, dict):
            return (data.get("ids") or [data.get("id")])[0]
        return data

    def _json2_call_method(
        self,
        model: str,
        method: str,
        ids: list[int] | None = None,
        kwargs: dict | None = None,
    ) -> Any:
        url = f"{self.url}/json/2/{model}/{method}"
        body = dict(kwargs or {})
        if ids:
            body["ids"] = ids
        response = self._client.post(url, json=body)
        response.raise_for_status()
        return response.json() if response.content else None

    def _json2_read(self, model: str, ids: list[int], fields: list[str]) -> list[dict]:
        body = {"ids": ids, "fields": fields}
        response = self._client.post(
            f"{self.url}/json/2/{model}/read",
            json=body,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and "records" in data:
            return data["records"]
        if isinstance(data, list):
            return data
        return [data]

    def _is_json2(self) -> bool:
        """True wanneer de huidige sessie expliciet in JSON-2 modus draait."""
        return bool(self._use_json2)

    def _detect_version(self) -> int:
        """ Versie ophalen """
        url = f"{self.url}/web/version"
        try:
            response = self._client.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning("Kon Odoo-versie niet ophalen - versiecheck overgeslagen. (%s)", exc)
            return 0

        version_info = data.get("version_info", [])
        raw = str(version_info[0]) if version_info else data.get("version", "")
        match = re.search(r"\d+", raw)
        major = int(match.group()) if match else 0

        logger.info(
            "Odoo versie gedetecteerd: %s (major=%s)",
            data.get("version", "onbekend"),
            major,
        )
        return major

    # 1. AUTHENTICATIE
    def authenticate(self) -> None:
        """
        Detecteer de Odoo-versie.
        v14–v18: sessie-gebaseerd via /web/session/authenticate (username + password).
        v19+:    stateless via Bearer token (ODOO_API_KEY).
        """
        self.odoo_version = self._detect_version()
        # Odoo URL check
        if not self.url:
            raise RuntimeError("ODOO_URL ontbreekt. Stel ODOO_URL in.")
        # Versie check
        if 0 < self.odoo_version < _MIN_VERSION:
            raise RuntimeError(
                f"Odoo v{self.odoo_version} is te oud. Minimale versie: v{_MIN_VERSION}."
            )

        if self.odoo_version and self.odoo_version >= _REST_API_VERSION and self.api_key:
            if not self.db:
                raise RuntimeError(
                    "ODOO_DB ontbreekt voor Odoo v19 JSON-2. "
                    "Stel ODOO_DB in (bijv. 'postgres') in."
                )
            self._client.headers.update(
                {
                    "Authorization": f"Bearer {self.api_key}",
                    "X-Odoo-Database": self.db,
                }
            )
            self.uid = 0
            self._use_json2 = True
            logger.info("Odoo v%s — REST API (JSON-2) modus via API key.", self.odoo_version)
            return

        if self.db and self.username and self.password:
            logger.info("Odoo authenticatie via JSON-RPC voor database %s.", self.db)
            result = self._post(
                "/web/session/authenticate",
                {"db": self.db, "login": self.username, "password": self.password},
            )
            if not result or not result.get("uid"):
                raise RuntimeError(
                    "Odoo authenticatie mislukt – controleer ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD."
                )
            self.uid = result["uid"]
            self._use_json2 = False
            return

        if self.odoo_version and self.odoo_version >= _REST_API_VERSION:
            # v19+: Bearer token auth – API key wordt als header op de client gezet
            if not self.api_key:
                raise RuntimeError(
                    "ODOO_API_KEY is vereist voor Odoo v19+. "
                )
            if not self.db:
                raise RuntimeError(
                    "ODOO_DB ontbreekt voor Odoo v19 JSON-2. "
                    "Stel ODOO_DB in (bijv. 'postgres') in."
                )

            self._client.headers.update(
                {
                    "Authorization": f"Bearer {self.api_key}",
                    "X-Odoo-Database": self.db,
                }
            )
            self.uid = 0  # Geen sessie-uid bij API-key auth
            self._use_json2 = True
            logger.info("Odoo v%s — JSON-2 modus via API key.", self.odoo_version)
        else:
            # v14–v18: sessie-gebaseerde auth via username + password
            if not self.db or not self.username or not self.password:
                raise RuntimeError(
                    "Voor Odoo v14-v18 zijn ODOO_DB, ODOO_USERNAME en ODOO_PASSWORD vereist."
                )
            logger.info("Odoo v%s — JSON-RPC 2.0 modus.", self.odoo_version)
            result = self._post(
                "/web/session/authenticate",
                {"db": self.db, "login": self.username, "password": self.password},
            )
            if not result or not result.get("uid"):
                raise RuntimeError(
                    "Odoo authenticatie mislukt – controleer ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD."
                )
            self.uid = result["uid"]
            self._use_json2 = False

    # ORDER SYNC: CUSTOMER MANAGEMENT
    def find_or_create_customer(
        self,
        billing: WooBillingAddress,
        customer_name: str,
        company_id: int | None = None,
        is_company: bool | None = None,
    ) -> int:
        """
        Zoek klant via e-mail.
        Maakt nieuwe klant aan als er geen match gevonden wordt.
        Geeft het partner_id terug.
        """
        self._validate_customer_data(billing, customer_name)
        if self._is_json2():
            return self._find_or_create_customer_json2(
                billing,
                customer_name,
                company_id,
                is_company=is_company,
            )
        return self._find_or_create_customer_jsonrpc(
            billing,
            customer_name,
            company_id,
            is_company=is_company,
        )

    def resolve_customer_partners(
        self,
        billing: WooBillingAddress,
        customer_name: str,
        shipping: WooBillingAddress | None = None,
        company_id: int | None = None,
        is_company: bool | None = None,
        create_delivery_children: bool = True,
        create_delivery_address: bool = True,
    ) -> dict[str, int]:
        """
        Bepaal hoofdpartner en optioneel afleveradres.

        Kort:
        - zoekt of maakt de hoofdklant (`partner_id`) op basis van het factuuradres.
        - maakt, indien toegestaan en nodig, een child-partner voor het afleveradres (`partner_shipping_id`).

        Args:
            billing: Factuuradres WooCommerce-order.
            customer_name: Naam van klant.
            shipping: Optioneel afleveradres.
            company_id: Odoo company-id.
            is_company: Geef aan of het om een bedrijf gaat of persoon.
            create_delivery_children: Bij True mag er een child-adres aangemaakt worden als adres verschilt.
            create_delivery_address: Bedrijfsinstelling die afleveradrescreatie toestaat.

        """
        partner_id = self.find_or_create_customer(
            billing,
            customer_name,
            company_id=company_id,
            is_company=is_company,
        )
        shipping_partner_id = partner_id

        # Gate delivery child creation on both create_delivery_children and create_delivery_address flags
        if create_delivery_children and create_delivery_address and shipping and self._has_meaningful_address(shipping) and not self._addresses_match(
            billing.address_1,
            billing.city,
            billing.postcode,
            shipping.address_1,
            shipping.city,
            shipping.postcode,
        ):
            self._validate_delivery_address(shipping)
            shipping_name = customer_name
            if shipping.city:
                shipping_name = f"{customer_name} - {shipping.city}"

            existing_delivery = self.find_delivery_address(
                partner_id,
                shipping.address_1,
                shipping.city,
                shipping.postcode,
            )
            if existing_delivery:
                shipping_partner_id = existing_delivery["id"]
            else:
                shipping_partner_id = self.create_delivery_address(
                    parent_partner_id=partner_id,
                    street=shipping.address_1,
                    city=shipping.city,
                    postcode=shipping.postcode,
                    country_code=shipping.country or billing.country or "NL",
                    address_name=shipping_name,
                )

        return {
            "partner_id": partner_id,
            "partner_shipping_id": shipping_partner_id,
        }

    @staticmethod
    def _normalize_text(value: str | None) -> str:
        return re.sub(r"\s+", " ", (value or "")).strip()

    @classmethod
    def _has_meaningful_address(cls, address: WooBillingAddress | None) -> bool:
        if not address:
            return False
        return any(
            cls._normalize_text(value)
            for value in (
                address.address_1,
                address.city,
                address.postcode,
                address.country,
            )
        )

    @classmethod
    def _validate_customer_data(cls, billing: WooBillingAddress, customer_name: str) -> None:
        missing_fields: list[str] = []

        if not cls._normalize_text(customer_name):
            missing_fields.append("naam")
        if not cls._normalize_text(billing.email):
            missing_fields.append("email")
        if not cls._normalize_text(billing.address_1):
            missing_fields.append("factuuradres")
        if not cls._normalize_text(billing.city):
            missing_fields.append("plaats")
        if not cls._normalize_text(billing.postcode):
            missing_fields.append("postcode")
        if not cls._normalize_text(billing.country):
            missing_fields.append("land")

        if missing_fields:
            missing_text = ", ".join(missing_fields)
            raise ValueError(
                _(
                    "Onvoldoende klantgegevens om een klant aan te maken: %s"
                )
                % missing_text
            )

    @classmethod
    def _validate_delivery_address(cls, shipping: WooBillingAddress) -> None:
        missing_fields: list[str] = []

        if not cls._normalize_text(shipping.address_1):
            missing_fields.append("leveradres")
        if not cls._normalize_text(shipping.city):
            missing_fields.append("plaats")
        if not cls._normalize_text(shipping.postcode):
            missing_fields.append("postcode")

        if missing_fields:
            missing_text = ", ".join(missing_fields)
            raise ValueError(
                _(
                    "Onvoldoende leveradresgegevens om een afleveradres aan te maken: %s"
                )
                % missing_text
            )

    # Klant zoeken / aanmaken via JSON-RPC 2.0 
    def _find_or_create_customer_jsonrpc(
        self,
        billing: WooBillingAddress,
        customer_name: str,
        company_id: int | None = None,
        is_company: bool | None = None,
    ) -> int:
        results: list[dict] = self._call_kw(
            "res.partner",
            "search_read",
            [[['email', '=ilike', billing.email], ['parent_id', '=', False]]],
            {
                "fields": [
                    "id",
                    "name",
                    "email",
                    "company_id",
                    "company_name",
                    "is_company",
                    "company_type",
                    "vat",
                    "street",
                    "street2",
                    "city",
                    "zip",
                    "country_id",
                    "company_id",
                ],
                "limit": 10,
            },
        )
        if results:
            partner_match = None
            for partner in results:
                partner_company = partner.get("company_id")
                partner_company_id = partner_company[0] if isinstance(partner_company, (list, tuple)) and partner_company else None
                if company_id is None or partner_company_id in (None, company_id):
                    partner_match = partner
                    break
            if partner_match is None:
                partner_match = results[0]

            partner_id = partner_match["id"]
            update_vals = self._build_partner_vals(
                billing,
                customer_name,
                is_company=is_company,
            )
            country_ids: list[int] = self._call_kw(
                "res.country", "search", [[["code", "=", billing.country.upper()]]]
            )
            if country_ids:
                update_vals["country_id"] = country_ids[0]
            update_vals = self._apply_existing_partner_update_policy(
                partner_match,
                update_vals,
            )
            self._call_kw("res.partner", "write", [[partner_id], update_vals])
            logger.info(
                "Bestaande Odoo klant gevonden en bijgewerkt id=%s voor email=%s",
                partner_id,
                billing.email,
            )
            return partner_id

        vals = self._build_partner_vals(
            billing,
            customer_name,
            is_company=is_company,
        )
        if company_id:
            vals["company_id"] = company_id
        country_ids: list[int] = self._call_kw(
            "res.country", "search", [[["code", "=", billing.country.upper()]]]
        )
        if country_ids:
            vals["country_id"] = country_ids[0]

        partner_id: int = self._call_kw("res.partner", "create", [vals])
        logger.info("Nieuwe Odoo klant aangemaakt id=%s naam=%s.", partner_id, customer_name)
        return partner_id
    # Klant zoeken / aanmaken via JSON-2 
    def _find_or_create_customer_json2(
        self,
        billing: WooBillingAddress,
        customer_name: str,
        company_id: int | None = None,
        is_company: bool | None = None,
    ) -> int:
        records = self._json2_search_read(
            "res.partner",
            [["email", "=ilike", billing.email], ["parent_id", "=", False]],
            [
                "id",
                "name",
                "email",
                "company_id",
                "company_name",
                "is_company",
                "company_type",
                "vat",
                "street",
                "street2",
                "city",
                "zip",
                "country_id",
                "company_id",
            ],
            limit=10,
        )
        if records:
            partner_match = None
            for partner in records:
                partner_company = partner.get("company_id")
                partner_company_id = partner_company[0] if isinstance(partner_company, (list, tuple)) and partner_company else None
                if company_id is None or partner_company_id in (None, company_id):
                    partner_match = partner
                    break
            if partner_match is None:
                partner_match = records[0]

            partner_id = partner_match["id"]
            update_vals = self._build_partner_vals(
                billing,
                customer_name,
                is_company=is_company,
            )
            country_ids = self._json2_search(
                "res.country", [["code", "=", billing.country.upper()]]
            )
            if country_ids:
                update_vals["country_id"] = country_ids[0]
            update_vals = self._apply_existing_partner_update_policy(
                partner_match,
                update_vals,
            )
            self._json2_call_method(
                "res.partner",
                "write",
                ids=[partner_id],
                kwargs={"vals": update_vals},
            )
            logger.info(
                "Bestaande Odoo klant gevonden en bijgewerkt id=%s voor email=%s",
                partner_id,
                billing.email,
            )
            return partner_id

        vals = self._build_partner_vals(
            billing,
            customer_name,
            is_company=is_company,
        )
        if company_id:
            vals["company_id"] = company_id
        country_ids = self._json2_search(
            "res.country", [["code", "=", billing.country.upper()]]
        )
        if country_ids:
            vals["country_id"] = country_ids[0]

        partner_id = self._json2_create("res.partner", vals)
        logger.info("Nieuwe Odoo klant aangemaakt id=%s naam=%s.", partner_id, customer_name)
        return partner_id

    @staticmethod
    def _build_partner_vals(
        billing: WooBillingAddress,
        customer_name: str,
        is_company: bool | None = None,
    ) -> dict:
        vals: dict[str, Any] = {
            "name": customer_name,
            "email": billing.email,
            "phone": billing.phone,
            "street": billing.address_1,
            "street2": billing.address_2,
            "city": billing.city,
            "zip": billing.postcode,
            "customer_rank": 1,
        }
        if billing.company:
            vals["company_name"] = billing.company
        if is_company is True:
            vals["company_type"] = "company"
            vals["is_company"] = True
        elif billing.company or billing.vat:
            vals["company_type"] = "company"
            vals["is_company"] = True
        if billing.vat:
            vals["vat"] = billing.vat
        if is_company is not True and not (billing.company or billing.vat):
            vals["company_type"] = "person"
            vals["is_company"] = False
        return vals

    @staticmethod
    def _addresses_match(
        street_a: str | None,
        city_a: str | None,
        zip_a: str | None,
        street_b: str | None,
        city_b: str | None,
        zip_b: str | None,
    ) -> bool:
        def normalize(value: str | None) -> str:
            return re.sub(r"\s+", " ", (value or "")).strip().lower()

        return (
            normalize(street_a) == normalize(street_b)
            and normalize(city_a) == normalize(city_b)
            and normalize(zip_a) == normalize(zip_b)
        )

    @staticmethod
    def _is_company_partner(partner: dict[str, Any] | None) -> bool:
        if not partner:
            return False
        if bool(partner.get("is_company")):
            return True
        return str(partner.get("company_type") or "").strip().lower() == "company"

    @staticmethod
    def _has_value(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, tuple, dict, set)):
            return bool(value)
        return True

    def _apply_existing_partner_update_policy(
        self,
        existing_partner: dict[str, Any],
        update_vals: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Bescherm kernidentiteitsvelden van bestaande bedrijfspartners.
        Behoud de gegevens stabiel en voeg leveringscontacten toe voor alternatieve verzending.
        """
        if not self._is_company_partner(existing_partner):
            return update_vals

        sanitized_vals = dict(update_vals)
        locked_fields = (
            "name",
            "company_name",
            "vat",
            "street",
            "street2",
            "city",
            "zip",
            "country_id",
        )

        for field_name in locked_fields:
            existing_value = existing_partner.get(field_name)
            new_value = sanitized_vals.get(field_name)
            if self._has_value(existing_value) and self._has_value(new_value):
                sanitized_vals.pop(field_name, None)

        return sanitized_vals

    # ORDER SYNC: ORDER DEDUPLICATION
    def order_exists(self, woo_order_number: str, company_id: int | None = None) -> bool:
        """
        Controleert of er al een sale.order bestaat met WooCommerce ordernummer.
        Gebruikt het 'note' veld.
        Voorkomt dubbele imports bij herhaaldelijk draaien van het script.
        """
        note_value = f"WooCommerce Order #{woo_order_number}"
        domain: list[list[Any]] = [["note", "ilike", note_value]]
        if company_id:
            domain.append(["company_id", "=", company_id])
        if self._is_json2():
            ids = self._json2_search("sale.order", domain)
        else:
            ids = self._call_kw("sale.order", "search", [domain])
        return bool(ids)

    # ORDER SYNC: PRODUCT LOOKUP
    def find_product_by_sku(self, sku: str, company_id: int | None = None) -> int | None:
        """
        Zoek product op via SKU (default_code veld).
        Geeft het product-id terug, of None als het product niet gevonden wordt.
        """
        if not sku:
            return None
        if self._is_json2():
            records = self._json2_search_read(
                "product.product",
                [["default_code", "=", sku], ["active", "=", True]],
                ["id", "name", "company_id"],
                limit=20,
            )
            if not records:
                return None

            if company_id is None:
                return records[0]["id"]

            for product in records:
                product_company = product.get("company_id")
                product_company_id = product_company[0] if isinstance(product_company, (list, tuple)) and product_company else None
                if product_company_id in (None, company_id):
                    return product["id"]
            return None
        results: list[dict] = self._call_kw(
            "product.product",
            "search_read",
            [[["default_code", "=", sku], ["active", "=", True]]],
            {"fields": ["id", "name", "company_id"], "limit": 20},
        )
        if not results:
            return None

        if company_id is None:
            return results[0]["id"]

        for product in results:
            product_company = product.get("company_id")
            product_company_id = product_company[0] if isinstance(product_company, (list, tuple)) and product_company else None
            if product_company_id in (None, company_id):
                return product["id"]
        return None

    # ORDER SYNC: HELPERS (CURRENCY, JOURNALS)
    def get_currency_id(self, code: str) -> int:
        """Zoek currency id op via ISO-code."""
        if self._is_json2():
            records = self._json2_search_read(
                "res.currency", [["name", "=", code.upper()]], ["id"], limit=1
            )
            if not records:
                raise RuntimeError(
                    f"Valuta '{code}' niet gevonden in Odoo. "
                    "Activeer hem via Accounting → Valuta's."
                )
            return records[0]["id"]
        results: list[dict] = self._call_kw(
            "res.currency",
            "search_read",
            [[["name", "=", code.upper()]]],
            {"fields": ["id"], "limit": 1},
        )
        if not results:
            raise RuntimeError(
                f"Valuta '{code}' niet gevonden in Odoo. "
                "Activeer hem via Accounting → Valuta's."
            )
        return results[0]["id"]

    # ORDER SYNC: SALE ORDER CREATION
    def create_sale_order(self, vals: dict) -> int:
        """
        Maak een order aan met de opgegeven vals-dict.
        Geeft het nieuwe order-id terug.
        """
        if self._is_json2():
            order_id = self._json2_create("sale.order", vals)
        else:
            order_id = self._call_kw("sale.order", "create", [vals])
        logger.info("sale.order aangemaakt id=%s.", order_id)
        return order_id
    
    # Meerdere orderregels aanmaken voor een bestaande order
    def create_sale_order_lines(
        self,
        order_id: int,
        lines: list[dict],
        company_id: int | None = None,
    ) -> list[int]:
        """
        Maak meerdere orderregels aan voor een bestaande order.
        
        Args:
            order_id: ID van de order
            lines: List van dicts met orderregel velden
                (product_id, product_uom_qty, price_unit, etc.)

        """
        line_ids: list[int] = []
        
        for line_data in lines:
            try:
                # Voegt order_id reference aan elke orderregel.
                # verplicht company_id, zodat de regels altijd in de juiste company worden aangemaakt.
                line_vals = {**line_data, "order_id": order_id}
                if company_id:
                    line_vals["company_id"] = company_id
                
                if self._is_json2():
                    line_id = self._json2_create("sale.order.line", line_vals)
                else:
                    line_id = self._call_kw("sale.order.line", "create", [line_vals])
                
                line_ids.append(line_id)
                logger.info(
                    "sale.order.line aangemaakt id=%s (product_id=%s, qty=%s)",
                    line_id,
                    line_data.get("product_id"),
                    line_data.get("product_uom_qty"),
                )
            except Exception as exc:
                logger.error(
                    "Fout bij aanmaken sale.order.line voor order id=%s: %s",
                    order_id,
                    exc,
                )
                raise
        
        return line_ids

    # ORDER SYNC: SO Nummer ophalen
    def get_sale_order_number(self, order_id: int) -> str | None:
        """
        Haal de SO nummer (S-nummer) op van een bestaande order.
        Geeft "S-000123" terug of None bij een fout.
        """
        try:
            if self._is_json2():
                records = self._json2_read("sale.order", [order_id], ["name"])
            else:
                records = self._call_kw(
                    "sale.order", "read", [[order_id]], {"fields": ["name"]}
                )
            
            if records and len(records) > 0:
                s_number = records[0].get("name")
                logger.info("sale.order id=%s → S-nummer=%s", order_id, s_number)
                return s_number
        except Exception as exc:
            logger.error(
                "Fout bij ophalen S-nummer van order id=%s: %s", order_id, exc
            )
        return None

    # ORDER SYNC: ORDER STATUS CHANGES
    def confirm_order(self, order_id: int) -> None:
        """Bevestig de order --> status wordt 'sale'."""
        if self._is_json2():
            if not create_picking:
                self._json2_call_method("sale.order", "action_confirm", ids=[order_id], kwargs={"context": {"no_procurement": True}})
            else:
                self._json2_call_method("sale.order", "action_confirm", ids=[order_id])
        else:
            if not create_picking:
                self._call_kw("sale.order", "action_confirm", [[order_id]], {"context": {"no_procurement": True}},)
            else:
                self._call_kw("sale.order", "action_confirm", [[order_id]])
        logger.info("Order id=%s bevestigd.", order_id)

        

    # Annuleer leverbonnen die automatisch zijn aangemaakt
    def cancel_delivery_pickings(self, order_id: int) -> None:
        """Annuleer automatisch aangemaakte leverbonnen als Delivery Picking Flow uit staat."""
        order_name = self.get_sale_order_number(order_id) or str(order_id)
        domain = [["origin", "=ilike", order_name], ["picking_type_code", "=", "outgoing"]]

        if self._is_json2():
            pickings = self._json2_search_read(
                "stock.picking", domain, ["id", "name", "state"], suppress_not_found=True
            )
            for picking in pickings:
                if picking.get("state") not in ("done", "cancel"):
                    self._json2_call_method("stock.picking", "action_cancel", ids=[picking["id"]])
                    logger.info("Leverbon id=%s geannuleerd (Delivery Picking Flow uit)", picking["id"])
        else:
            pickings = self._call_kw(
                "stock.picking", "search_read", [domain], {"fields": ["id", "name", "state"]}
            )
            for picking in pickings:
                if picking.get("state") not in ("done", "cancel"):
                    self._call_kw("stock.picking", "action_cancel", [[picking["id"]]])
                    logger.info("Leverbon id=%s geannuleerd (Delivery Picking Flow uit)", picking["id"])

    def reserve_delivery_picking(self, picking_id: int) -> None:
        """Reserveer voorraad voor een leverbon → status wordt Waiting."""
        try:
            if self._is_json2():
                self._json2_call_method("stock.picking", "action_assign", ids=[picking_id])
            else:
                self._call_kw("stock.picking", "action_assign", [[picking_id]])
            logger.info("Voorraad gereserveerd voor leverbon id=%s", picking_id)
        except Exception as exc:
            logger.warning("Voorraadreservering mislukt voor picking id=%s: %s", picking_id, exc)

    def create_delivery_picking(self, order_id: int, reserve_stock: bool = False) -> int | None:
        """
        Maak een leverbon aan voor een bevestigde order.
        
        Args:
            order_id:      ID van de bevestigde order
            reserve_stock: Als True, reserveer voorraad
        
        Returns:
            picking_id of None bij fout
        """
        try:
            # Odoo maakt automatisch pickings aan bij confirm — zoek ze op
            order_name = self.get_sale_order_number(order_id) or str(order_id)
            domain = [["origin", "=ilike", order_name], ["picking_type_code", "=", "outgoing"]]

            if self._is_json2():
                pickings = self._json2_search_read(
                    "stock.picking", domain, ["id", "name", "state"], suppress_not_found=True
                )
            else:
                pickings = self._call_kw(
                    "stock.picking", "search_read", [domain], {"fields": ["id", "name", "state"]}
                )

            if not pickings:
                logger.warning("Geen leverbon gevonden voor order %s na bevestiging.", order_name)
                return None

            picking_id = pickings[0]["id"]
            picking_state = pickings[0].get("state", "")
            logger.info("Leverbon id=%s gevonden (state=%s) voor order %s", picking_id, picking_state, order_name)

            # Reserveer stock alleen als de checkbox aan staat
            if reserve_stock and picking_state in ("confirmed", "waiting"):
                try:
                    if self._is_json2():
                        self._json2_call_method("stock.picking", "action_assign", ids=[picking_id])
                    else:
                        self._call_kw("stock.picking", "action_assign", [[picking_id]])
                    logger.info("Voorraad gereserveerd voor leverbon id=%s", picking_id)
                except Exception as exc:
                    logger.warning("Voorraadreservering mislukt voor picking id=%s: %s", picking_id, exc)
            elif not reserve_stock:
                logger.info("Leverbon id=%s aangemaakt zonder stockreservering (woo_track_stock=False)", picking_id)

            return picking_id

        except Exception as exc:
            logger.error("Fout bij ophalen/verwerken leverbon voor order id=%s: %s", order_id, exc)
            return None

    def keep_order_deliveries_unreserved(self, order_id: int) -> int:
        """
        Zet leveringen van een bevestigde order op 'beschikbaar'.
        Dit behoudt de order status op 'sale', maar haalt reservering weg
        zodat magazijn de leverbon eerst kan controleren.
        """
        order_name = self.get_sale_order_number(order_id) or str(order_id)
        domain = [["origin", "=ilike", order_name], ["state", "=", "assigned"]]
        unreserved = 0

        unreserve_methods = ("do_unreserve", "action_unreserve")

        if self._is_json2():
            try:
                pickings = self._json2_search_read(
                    "stock.picking",
                    domain,
                    ["id", "name", "state"],
                    suppress_not_found=True,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    logger.info(
                        "Stock module niet beschikbaar in Odoo (model stock.picking ontbreekt). "
                        "Stap 'unreserve deliveries' wordt overgeslagen."
                    )
                    return 0
                raise
            for picking in pickings:
                picking_id = picking["id"]
                method_ok = False
                for method_name in unreserve_methods:
                    try:
                        self._json2_call_method("stock.picking", method_name, ids=[picking_id])
                        method_ok = True
                        break
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code == 404:
                            continue
                        raise
                if method_ok:
                    unreserved += 1
                    logger.info(
                        "Leverbon %s (id=%s) op unreserved gezet na orderbevestiging.",
                        picking.get("name", "?"),
                        picking_id,
                    )
                else:
                    logger.warning(
                        "Geen ondersteunde unreserve-methode gevonden voor leverbon %s (id=%s).",
                        picking.get("name", "?"),
                        picking_id,
                    )
        else:
            pickings = self._call_kw(
                "stock.picking",
                "search_read",
                [domain],
                {"fields": ["id", "name", "state"]},
            )
            for picking in pickings:
                picking_id = picking["id"]
                method_ok = False
                for method_name in unreserve_methods:
                    try:
                        self._call_kw("stock.picking", method_name, [[picking_id]])
                        method_ok = True
                        break
                    except Exception:
                        continue
                if method_ok:
                    unreserved += 1
                    logger.info(
                        "Leverbon %s (id=%s) op unreserved gezet na orderbevestiging.",
                        picking.get("name", "?"),
                        picking_id,
                    )
                else:
                    logger.warning(
                        "Geen ondersteunde unreserve-methode gevonden voor leverbon %s (id=%s).",
                        picking.get("name", "?"),
                        picking_id,
                    )

        if unreserved == 0:
            logger.debug(
                "Geen assigned leverbons gevonden om te unreserven voor sale.order id=%s.",
                order_id,
            )

        return unreserved

    def reset_order_deliveries_to_draft(self, order_id: int) -> int:
        """
        Reset leveringen van een bevestigde order naar draft.

        Voor betaalde orders willen we wel leverbonnen kunnen aanmaken, maar niet
        automatisch reserveren/afhandelen. Daarom worden de gekoppelde stock.moves
        teruggezet naar draft nadat de reservering is vrijgegeven.
        """
        order_name = self.get_sale_order_number(order_id) or str(order_id)
        domain = [["origin", "=ilike", order_name], ["state", "in", ["confirmed", "waiting", "assigned", "partially_available"]]]
        reset_count = 0

        if self._is_json2():
            try:
                pickings = self._json2_search_read(
                    "stock.picking",
                    domain,
                    ["id", "name", "state"],
                    suppress_not_found=True,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    logger.info(
                        "Stock module niet beschikbaar in Odoo (model stock.picking ontbreekt). "
                        "Stap 'reset deliveries to draft' wordt overgeslagen."
                    )
                    return 0
                raise

            if not pickings:
                # TODO: PHASE 8 fallback delivery creation temporarily disabled (out-of-scope)
                # created = self._create_draft_delivery_for_order(order_id, order_name)
                # if created:
                #     logger.info(
                #         "Geen leverbon gevonden voor order %s. Draft leverbon automatisch aangemaakt.",
                #         order_name,
                #     )
                #     return 1
                logger.info(
                    "Geen leverbon gevonden voor order %s. Fallback leverbon creatie uitgeschakeld (out-of-scope).",
                    order_name,
                )
                return 0

            for picking in pickings:
                picking_id = picking["id"]
                try:
                    moves = self._json2_search_read(
                        "stock.move",
                        [["picking_id", "=", picking_id], ["state", "!=", "draft"]],
                        ["id", "state"],
                    )
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        logger.info(
                            "Stock module niet beschikbaar in Odoo (model stock.move ontbreekt). "
                            "Stap 'reset deliveries to draft' wordt overgeslagen."
                        )
                        return reset_count
                    raise

                if not moves:
                    continue

                # First unreserve existing quants where possible.
                for move in moves:
                    move_id = move["id"]
                    for method_name in ("_do_unreserve", "do_unreserve", "action_unreserve"):
                        try:
                            self._json2_call_method("stock.move", method_name, ids=[move_id])
                            break
                        except httpx.HTTPStatusError as exc:
                            if exc.response.status_code == 404:
                                continue
                            # If the method exists but fails because the move is not reserved,
                            # continue to the next method or the draft reset.
                            continue

                    try:
                        self._json2_call_method(
                            "stock.move",
                            "write",
                            ids=[move_id],
                            kwargs={"vals": {"state": "draft"}},
                        )
                    except Exception as exc:
                        logger.warning(
                            "Kon stock.move id=%s niet naar draft zetten voor picking id=%s: %s",
                            move_id,
                            picking_id,
                            exc,
                        )
                        continue

                reset_count += 1
                logger.info(
                    "Leverbon %s (id=%s) teruggezet naar draft na orderbevestiging.",
                    picking.get("name", "?"),
                    picking_id,
                )

        return reset_count

    def _create_draft_delivery_for_order(self, order_id: int, order_name: str) -> bool:
        """
        Maak een draft leverbon aan voor een order als er geen picking bestaat.
        """
        if not self._is_json2():
            return False

        try:
            order_records = self._json2_read(
                "sale.order",
                [order_id],
                ["id", "name", "partner_id", "company_id", "order_line"],
            )
        except Exception as exc:
            logger.warning("Kon sale.order %s niet lezen voor leverbon fallback: %s", order_id, exc)
            return False

        if not order_records:
            return False

        order = order_records[0]
        partner = order.get("partner_id")
        partner_id = partner[0] if isinstance(partner, (list, tuple)) and partner else None
        company = order.get("company_id")
        company_id = company[0] if isinstance(company, (list, tuple)) and company else None
        line_ids = order.get("order_line") or []

        if not line_ids:
            logger.info("Order %s heeft geen lijnen; geen leverbon aangemaakt.", order_name)
            return False

        # Validate line_ids before retrieval
        try:
            line_ids = [int(lid) for lid in line_ids]
        except (TypeError, ValueError):
            logger.warning("Order %s heeft ongeldige line_ids; leverbon fallback afgebroken.", order_name)
            return False

        try:
            picking_types = self._json2_search_read(
                "stock.picking.type",
                [["code", "=", "outgoing"], ["company_id", "=", company_id]],
                ["id", "default_location_src_id", "default_location_dest_id"],
                limit=1,
                suppress_not_found=True,
            )
            if not picking_types:
                picking_types = self._json2_search_read(
                    "stock.picking.type",
                    [["code", "=", "outgoing"]],
                    ["id", "default_location_src_id", "default_location_dest_id"],
                    limit=1,
                    suppress_not_found=True,
                )
        except Exception as exc:
            logger.warning("Kon stock.picking.type niet ophalen voor order %s: %s", order_name, exc)
            return False

        if not picking_types:
            logger.warning("Geen outgoing picking type gevonden; leverbon fallback overgeslagen voor %s.", order_name)
            return False

        picking_type = picking_types[0]
        location_src = picking_type.get("default_location_src_id")
        location_dest = picking_type.get("default_location_dest_id")
        location_id = location_src[0] if isinstance(location_src, (list, tuple)) and location_src else None
        location_dest_id = location_dest[0] if isinstance(location_dest, (list, tuple)) and location_dest else None
        if not location_id or not location_dest_id:
            logger.warning("Picking type mist default locations; leverbon fallback overgeslagen voor %s.", order_name)
            return False

        picking_vals: dict[str, Any] = {
            "origin": order_name,
            "picking_type_id": picking_type["id"],
            "location_id": location_id,
            "location_dest_id": location_dest_id,
        }
        if partner_id:
            picking_vals["partner_id"] = partner_id
        if company_id:
            picking_vals["company_id"] = company_id

        try:
            picking_id = self._json2_create("stock.picking", picking_vals)
        except Exception as exc:
            logger.warning("Kon draft leverbon niet aanmaken voor order %s: %s", order_name, exc)
            return False

        # Resilient line retrieval with context logging and fallback strategy
        logger.debug(
            "Order %s (id=%s): leverbon fallback – proberen %d sale.order.line records op te halen",
            order_name,
            order_id,
            len(line_ids),
        )
        order_lines = self._get_order_lines_with_fallback(
            order_name,
            line_ids,
        )
        if not order_lines:
            return False

        created_moves = 0
        for line in order_lines:
            product = line.get("product_id")
            product_id = product[0] if isinstance(product, (list, tuple)) and product else None
            if not product_id:
                continue

            qty = float(line.get("product_uom_qty") or 0.0)
            if qty <= 0:
                continue

            uom = line.get("product_uom_id")
            product_uom = uom[0] if isinstance(uom, (list, tuple)) and uom else None
            if not product_uom:
                continue

            move_vals: dict[str, Any] = {
                "product_id": product_id,
                "product_uom_id": product_uom,
                "product_uom_qty": qty,
                "picking_id": picking_id,
                "location_id": location_id,
                "location_dest_id": location_dest_id,
                "state": "draft",
            }
            if company_id:
                move_vals["company_id"] = company_id

            try:
                self._json2_create("stock.move", move_vals)
                created_moves += 1
            except Exception as exc:
                logger.warning(
                    "Kon stock.move niet aanmaken voor picking id=%s line id=%s: %s",
                    picking_id,
                    line.get("id"),
                    exc,
                )

        if created_moves == 0:
            logger.warning("Draft leverbon aangemaakt maar zonder moves voor order %s.", order_name)
        else:
            logger.info(
                "Draft leverbon id=%s aangemaakt voor order %s met %s move(s).",
                picking_id,
                order_name,
                created_moves,
            )
        return True

    def _get_order_lines_with_fallback(self, order_name: str, line_ids: list[int]) -> list[dict]:
        """
        Robuuste ophalen van orderregels-records met een fallbackstrategie.
        """
        fields = ["id", "name", "product_id", "product_uom_id", "product_uom_qty"]
        
        # Attempt 1: Direct read(ids) – faster, but may fail if Odoo model has issues
        try:
            logger.debug("Order %s: poging 1 – sale.order.line/read met %d ids", order_name, len(line_ids))
            order_lines = self._json2_read("sale.order.line", line_ids, fields)
            logger.debug("Order %s: poging 1 geslaagd – %d lijnen opgehaald", order_name, len(order_lines))
            return order_lines
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 500:
                logger.debug(
                    "Order %s: poging 1 server error 500 – fallback naar search_read",
                    order_name,
                )
            else:
                logger.warning(
                    "Order %s: poging 1 HTTP error %s – fallback naar search_read",
                    order_name,
                    exc.response.status_code,
                )
        except Exception as exc:
            logger.warning(
                "Order %s: poging 1 onverwachte fout – fallback naar search_read: %s",
                order_name,
                exc,
            )
        
        # Attempt 2: Fallback via search_read with id in domain
        try:
            logger.debug(
                "Order %s: poging 2 – sale.order.line/search_read met id in (%d)",
                order_name,
                len(line_ids),
            )
            order_lines = self._json2_search_read(
                "sale.order.line",
                [["id", "in", line_ids]],
                fields,
            )
            logger.debug("Order %s: poging 2 geslaagd – %d lijnen opgehaald", order_name, len(order_lines))
            return order_lines
        except Exception as exc:
            logger.warning(
                "Order %s: beide line-retrieval pogingen mislukt (read + search_read). "
                "Fallback leverbon creatie afgebroken: %s",
                order_name,
                exc,
            )
            return []

    # ORDER SYNC: ORDER STATUS CHANGES (NIET ACTIEF IN GEBRUIK)
    def lock_order(self, order_id: int) -> None:
        """Vergrendel de sale.order (action_lock) → status wordt 'done'."""
        if self._is_json2():
            self._json2_call_method("sale.order", "action_lock", ids=[order_id])
        else:
            self._call_kw("sale.order", "action_lock", [[order_id]])
        logger.info("sale.order id=%s vergrendeld (done).", order_id)

    def cancel_order(self, order_id: int) -> None:
        """Annuleer de sale.order (action_cancel)."""
        if self._is_json2():
            self._json2_call_method("sale.order", "action_cancel", ids=[order_id])
        else:
            self._call_kw("sale.order", "action_cancel", [[order_id]])
        logger.info("sale.order id=%s geannuleerd.", order_id)

    # INVOICE CREATION & POSTING
    def create_invoice_from_order(self, order_id: int) -> int:
        """
        Maak een factuur aan vanuit een bevestigde order.
        Geeft het invoice-id (account.move) terug.
        """
        context = {
            "active_ids": [order_id],
            "active_model": "sale.order",
            "open_invoices": False,
        }
        wizard_vals = {"advance_payment_method": "delivered"}

        if self._is_json2():
            wizard_id = self._json2_create(
                "sale.advance.payment.inv", wizard_vals, context=context
            )
            self._json2_call_method(
                "sale.advance.payment.inv",
                "create_invoices",
                ids=[wizard_id],
                kwargs={"context": context},
            )
            records = self._json2_read("sale.order", [order_id], ["invoice_ids"])
            invoice_ids = records[0].get("invoice_ids", [])
        else:
            wizard_id = self._call_kw(
                "sale.advance.payment.inv",
                "create",
                [wizard_vals],
                {"context": context},
            )
            self._call_kw(
                "sale.advance.payment.inv",
                "create_invoices",
                [[wizard_id]],
                {"context": context},
            )
            order_read = self._call_kw(
                "sale.order", "read", [[order_id]], {"fields": ["invoice_ids"]}
            )
            invoice_ids = order_read[0]["invoice_ids"] if order_read else []

        if not invoice_ids:
            raise RuntimeError(
                f"Geen factuur aangemaakt voor sale.order id={order_id}. "
                "Controleer of alle orderregels een leverbaar product hebben."
            )

        invoice_id = invoice_ids[-1]  # Meest recente factuur
        logger.info("Factuur id=%s aangemaakt voor sale.order id=%s.", invoice_id, order_id)
        return invoice_id

    def post_invoice(self, invoice_id: int) -> None:
        """Bevestig draft factuur --> status wordt 'posted'."""
        if self._is_json2():
            self._json2_call_method("account.move", "action_post", ids=[invoice_id])
        else:
            self._call_kw("account.move", "action_post", [[invoice_id]])
        logger.info("Factuur id=%s bevestigd (posted).", invoice_id)

    # PAYMENT REGISTRATION
    def register_payment(
        self,
        invoice_id: int,
        amount: float,
        journal_id: int,
        payment_date: str,
        transaction_id: str = "",
    ) -> None:
        """
        Registreer inkomende betaling voor een geposte factuur.

        Args:
            invoice_id:     Odoo account.move id.
            amount:         Betalingsbedrag.
            journal_id:     Odoo account.journal id (bijv. Stripe / Bank).
            payment_date:   Betalingsdatum (YYYY-MM-DD).
            transaction_id: Stripe Payment Intent ID – opgeslagen als referentie.
        """
        if self._is_json2():
            self._register_payment_json2(invoice_id, amount, journal_id, payment_date, transaction_id)
        else:
            self._register_payment_jsonrpc(invoice_id, amount, journal_id, payment_date, transaction_id)

    def _register_payment_jsonrpc(
        self,
        invoice_id: int,
        amount: float,
        journal_id: int,
        payment_date: str,
        transaction_id: str,
    ) -> None:
        context = {
            "active_model": "account.move",
            "active_ids": [invoice_id],
            "active_id": invoice_id,
        }
        wizard_vals: dict[str, Any] = {
            "journal_id": journal_id,
            "amount": amount,
            "payment_date": payment_date,
        }
        if transaction_id:
            wizard_vals["communication"] = transaction_id

        wizard_id: int = self._call_kw(
            "account.payment.register", "create", [wizard_vals], {"context": context}
        )
        self._call_kw(
            "account.payment.register",
            "action_create_payments",
            [[wizard_id]],
            {"context": context},
        )
        logger.info(
            "Betaling geregistreerd: %.2f voor factuur id=%s (ref=%s).",
            amount,
            invoice_id,
            transaction_id or "n/a",
        )

    def _register_payment_json2(
        self,
        invoice_id: int,
        amount: float,
        journal_id: int,
        payment_date: str,
        transaction_id: str,
    ) -> None:
        context = {
            "active_model": "account.move",
            "active_ids": [invoice_id],
            "active_id": invoice_id,
        }
        wizard_vals: dict[str, Any] = {
            "journal_id": journal_id,
            "amount": amount,
            "payment_date": payment_date,
        }
        if transaction_id:
            wizard_vals["communication"] = transaction_id

        wizard_id = self._json2_create("account.payment.register", wizard_vals, context=context)
        self._json2_call_method(
            "account.payment.register",
            "action_create_payments",
            ids=[wizard_id],
            kwargs={"context": context},
        )
        logger.info(
            "Betaling geregistreerd: %.2f voor factuur id=%s (ref=%s).",
            amount,
            invoice_id,
            transaction_id or "n/a",
        )

    # CREDIT NOTE CREATION (Niet in gebruik)
    def create_credit_note(self, invoice_id: int) -> int:
        """
        Maak een credit nota (refund) aan.
        Gebruik daarna post_invoice() om hem te bevestigen.
        Geeft het credit nota id terug.
        """
        context = {
            "active_model": "account.move",
            "active_ids": [invoice_id],
            "active_id": invoice_id,
        }
        wizard_vals = {
            "move_ids": [[4, invoice_id, 0]],
            "refund_method": "refund",  # aanmaken, niet meteen verrekenen
        }

        if self._is_json2():
            wizard_id = self._json2_create(
                "account.move.reversal", wizard_vals, context=context
            )
            result = self._json2_call_method(
                "account.move.reversal",
                "reverse_moves",
                ids=[wizard_id],
                kwargs={"context": context},
            )
        else:
            wizard_id = self._call_kw(
                "account.move.reversal", "create", [wizard_vals], {"context": context}
            )
            result = self._call_kw(
                "account.move.reversal",
                "reverse_moves",
                [[wizard_id]],
                {"context": context},
            )

        credit_note_id: int | None = None
        if isinstance(result, dict):
            credit_note_id = result.get("res_id")
            if not credit_note_id and "domain" in result:
                domain = result["domain"]
                if self._is_json2():
                    ids = self._json2_search("account.move", domain)
                else:
                    ids = self._call_kw("account.move", "search", [domain])
                credit_note_id = ids[0] if ids else None

        if not credit_note_id:
            raise RuntimeError(
                f"Kon credit nota niet aanmaken voor factuur id={invoice_id}."
            )

        logger.info("Credit nota id=%s aangemaakt voor factuur id=%s.", credit_note_id, invoice_id)
        return credit_note_id

    # PRODUCT SYNC: GET_PRODUCTS
    def get_products(self, batch_size: int = 100, company_id: int | None = None) -> list:
        """Haal alle actieve producten op uit Odoo met gedetecteerde pricelist discounts.
        
        Args:
            batch_size: Aantal records per API call
            company_id: Alleen producten van dit bedrijf ophalen
        """
        from .models import OdooProduct
        
        products: list[OdooProduct] = []
        fields = [
            "id", "name", "default_code", "barcode", "list_price",
            "description_sale", "qty_available", "categ_id", "product_tmpl_id"
        ]
        optional_sale_price_fields = ["woo_sale_price", "x_woo_sale_price", "sale_price"]
        optional_discount_fields = ["woo_discount_percent", "x_woo_discount_percent"]
        available_fields: set[str] = set()

        # Detecteer optionele kortingsvelden
        if self._is_json2():
            try:
                response = self._client.post(
                    f"{self.url}/json/2/product.product/fields_get",
                    json={"attributes": ["type"]},
                )
                response.raise_for_status()
                field_meta = response.json() or {}
                available_fields = set(field_meta.keys()) if isinstance(field_meta, dict) else set()
                fields.extend([f for f in optional_sale_price_fields if f in available_fields])
                fields.extend([f for f in optional_discount_fields if f in available_fields])
            except Exception as exc:
                logger.warning("Kon optionele kortingsvelden niet detecteren: %s", exc)
        else:
            try:
                field_meta = self._call_kw(
                    "product.product",
                    "fields_get",
                    [],
                    {"attributes": ["type"]},
                )
                available_fields = set(field_meta.keys()) if isinstance(field_meta, dict) else set()
                fields.extend([f for f in optional_sale_price_fields if f in available_fields])
                fields.extend([f for f in optional_discount_fields if f in available_fields])
            except Exception as exc:
                logger.warning("Kon optionele kortingsvelden niet detecteren: %s", exc)

        fields_fallback = [
            "id", "name", "default_code", "barcode", "list_price",
            "description_sale", "free_qty", "categ_id", "product_tmpl_id"
        ]
        fields_no_stock = [
            "id", "name", "default_code", "barcode", "list_price", "description_sale", "categ_id", "product_tmpl_id"
        ]
        
        # Filter: actieve producten + optioneel bedrijf filtering
        domain = [["active", "=", True]]
        if company_id:
            domain.append(["company_id", "=", company_id])
        
        logger.info("Producten ophalen...")
        
        if self._is_json2():
            offset = 0
            use_fallback_stock_field = "qty_available" not in available_fields and "free_qty" in available_fields
            use_no_stock_fields = "qty_available" not in available_fields and "free_qty" not in available_fields
            if use_fallback_stock_field:
                logger.info("qty_available ontbreekt; gebruik free_qty voor productsync")
            elif use_no_stock_fields:
                logger.info("Geen stock-velden beschikbaar; productsync gebruikt qty_available=0")
            while True:
                try:
                    records = self._json2_search_read(
                        "product.product",
                        domain,
                        fields_no_stock if use_no_stock_fields else (fields_fallback if use_fallback_stock_field else fields),
                        limit=batch_size,
                        offset=offset,
                    )
                except httpx.HTTPStatusError as exc:
                    error_text = (exc.response.text or "").lower()
                    if "qty_available" in error_text and not use_fallback_stock_field:
                        logger.warning("qty_available niet beschikbaar; fallback naar free_qty")
                        use_fallback_stock_field = True
                        continue
                    if "free_qty" in error_text and use_fallback_stock_field and not use_no_stock_fields:
                        logger.warning("Stock-velden niet beschikbaar; sync gaat verder zonder stock")
                        use_no_stock_fields = True
                        continue
                    raise

                if not records:
                    break
                for record in records:
                    try:
                        sale_value = None
                        for sale_field in optional_sale_price_fields:
                            if sale_field in record and record.get(sale_field) not in (None, False, ""):
                                sale_value = record.get(sale_field)
                                break
                        discount_value = None
                        for discount_field in optional_discount_fields:
                            if discount_field in record and record.get(discount_field) not in (None, False, ""):
                                discount_value = record.get(discount_field)
                                break
                        if sale_value is not None:
                            record["sale_price"] = sale_value
                        if discount_value is not None:
                            record["discount_percent"] = discount_value
                        if use_fallback_stock_field and "qty_available" not in record:
                            record["qty_available"] = record.get("free_qty", 0)
                        if use_no_stock_fields and "qty_available" not in record:
                            record["qty_available"] = 0
                        products.append(OdooProduct(**record))
                    except Exception as exc:
                        logger.warning("Product id=%s overgeslagen: %s", record.get("id"), exc)
                if len(records) < batch_size:
                    break
                offset += batch_size
        else:
            offset = 0
            while True:
                records: list[dict] = self._call_kw(
                    "product.product",
                    "search_read",
                    [domain],
                    {"fields": fields, "limit": batch_size, "offset": offset},
                )
                if not records:
                    break
                for record in records:
                    try:
                        sale_value = None
                        for sale_field in optional_sale_price_fields:
                            if sale_field in record and record.get(sale_field) not in (None, False, ""):
                                sale_value = record.get(sale_field)
                                break
                        discount_value = None
                        for discount_field in optional_discount_fields:
                            if discount_field in record and record.get(discount_field) not in (None, False, ""):
                                discount_value = record.get(discount_field)
                                break
                        if sale_value is not None:
                            record["sale_price"] = sale_value
                        if discount_value is not None:
                            record["discount_percent"] = discount_value
                        products.append(OdooProduct(**record))
                    except Exception as exc:
                        logger.warning("Product id=%s overgeslagen: %s", record.get("id"), exc)
                if len(records) < batch_size:
                    break
                offset += batch_size
        
        logger.info("Totaal %s producten opgehaald", len(products))
        logger.info("Pricelist auto-detect gestart...")
        pricelists_to_apply = self._auto_detect_pricelists(company_id=company_id)
        if pricelists_to_apply:
            pricelist_labels = ", ".join(
                f"'{pricelist['display_name']}' (id={pricelist['id']})"
                for pricelist in pricelists_to_apply
            )
            logger.info("Pricelists ACTIEF: %s", pricelist_labels)
            if products:
                for pricelist in pricelists_to_apply:
                    self._apply_pricelist_discounts(products, pricelist["id"])
        else:
            logger.info("Geen actieve pricelists gevonden")

        return products

    # PRODUCT SYNC: PRICELIST DISCOUNT ENGINE (JSON-2 API)
    def _auto_detect_pricelists(self, company_id: int | None = None) -> list[dict]:
        """
        Auto-detecteer alle relevante pricelists voor product discounts.
        
        Logica:
        1. Fetch ACTIVE pricelists (bedrijfsspecifiek + shared indien company_id is opgegeven)
        2. Filter op: item_ids (not empty) = pricelist met regels
        3. Retourneer alle matches, zodat meerdere lists gecombineerd kunnen worden
        
        Args:
            company_id: Optioneel bedrijf ID om pricelists op te filteren
        """
        if not self._is_json2():
            logger.info("Pricelist auto-detect alleen beschikbaar voor Odoo v19+ (JSON-2 API)")
            return []

        logger.debug("Auto-detecteren pricelist met items...")

        fields = ["id", "name", "display_name", "item_ids", "company_id"]
        domains: list[list[Any]] = []

        if company_id:
            # Neem zowel bedrijfsspecifieke als gedeelde pricelists mee.
            domains.append([["active", "=", True], ["company_id", "=", company_id]])
            domains.append([["active", "=", True], ["company_id", "=", False]])
        else:
            domains.append([["active", "=", True]])

        detected: dict[int, dict] = {}

        for domain in domains:
            try:
                pricelists = self._json2_search_read(
                    "product.pricelist",
                    domain,
                    fields,
                    limit=1000,
                )
            except Exception as exc:
                logger.error("Fout bij ophalen pricelists: %s", exc)
                continue

            logger.debug("Totaal %s active pricelist(s) gevonden voor domain=%s", len(pricelists), domain)

            for pricelist in pricelists:
                pricelist_id = pricelist.get("id")
                if not isinstance(pricelist_id, int):
                    continue

                item_ids = pricelist.get("item_ids", [])
                has_items = isinstance(item_ids, list) and len(item_ids) > 0

                logger.debug(
                    "  Pricelist id=%s name='%s' items=%d %s",
                    pricelist_id,
                    pricelist.get("display_name"),
                    len(item_ids) if isinstance(item_ids, list) else 0,
                    "✓ bruikbaar" if has_items else "✗ geen items",
                )

                if has_items and pricelist_id not in detected:
                    detected[pricelist_id] = {
                        "id": pricelist_id,
                        "name": pricelist.get("name"),
                        "display_name": pricelist.get("display_name"),
                    }

        pricelists = sorted(
            detected.values(),
            key=lambda item: ((item.get("display_name") or "").lower(), int(item.get("id") or 0)),
        )

        if pricelists:
            logger.info(
                "✓ %s active pricelist(en) met items gevonden",
                len(pricelists),
            )
            return pricelists

        logger.warning("Geen active pricelist met items gevonden")
        return []

    def _auto_detect_pricelist(self, company_id: int | None = None) -> dict | None:
        """Backward-compatible wrapper: retourneert de eerste pricelist, indien aanwezig."""
        pricelists = self._auto_detect_pricelists(company_id=company_id)
        return pricelists[0] if pricelists else None

    def _apply_pricelist_discounts(self, products: list, pricelist_id: int) -> None:
        """
        Pas korting van een Odoo pricelist toe op alle producten.
        Dit werkt alleen voor Odoo v19+ (JSON-2 API).
        
        Proces:
        1. Haal alle regels op (product-specifieke items) via server domain filtering
        2. Zet discounts om naar sale_price en discount_percent
        3. Update producten met sale prices
        
        Args:
            products: Lijst met OdooProduct objecten
            pricelist_id: ID van de pricelist die moet worden toegepast
        """
        if not self._is_json2():
            logger.info(
                "Pricelist discounts kunnen niet worden toegepast: Odoo v14-v18 ondersteunt dit nog niet. "
            )
            return

        logger.info("")
        logger.info(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        logger.info(" Pricelist (id=%s) toepassen op %s product(en)", pricelist_id, len(products))
        logger.info(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        try:
            # Stap 1: Haal regels op
            logger.info(" Ophalen pricelist rules...")
            rules = self._fetch_pricelist_rules(pricelist_id)
            if not rules:
                logger.warning(
                    "  Pricelist (id=%s) bevat geen product-specifieke regels!",
                    pricelist_id,
                )
                logger.info(
                    "  → Voeg rules toe: Sales → Pricelists → items (scope=Product of Product Variant)"
                )
                return
            logger.info("  %s regel(s) gevonden", len(rules))

            # Stap 2: Indexeer regels op product variant/template
            logger.info(" Indexeren regels op product/variant...")
            variant_rules: dict[int, dict] = {}
            template_rules: dict[int, dict] = {}
            
            for rule in rules:
                scope = str(rule.get("applied_on") or "")
                
                if scope == "0_product_variant":
                    variant_id = self._extract_m2o_id(rule.get("product_id"))
                    if variant_id:
                        variant_rules[variant_id] = rule
                        logger.debug(
                            "    Rule id=%s → variant %s (compute=%s)",
                            rule.get("id"),
                            variant_id,
                            rule.get("compute_price"),
                        )
                
                elif scope == "1_product":
                    template_id = self._extract_m2o_id(rule.get("product_tmpl_id"))
                    if template_id:
                        template_rules[template_id] = rule
                        logger.debug(
                            "    Rule id=%s → template %s (compute=%s)",
                            rule.get("id"),
                            template_id,
                            rule.get("compute_price"),
                        )
                else:
                    logger.debug(
                        "    Rule id=%s: onbekend scope '%s' – overgeslagen",
                        rule.get("id"),
                        scope,
                    )

            logger.info(
                "  %s variant regels, %s template regels",
                len(variant_rules),
                len(template_rules),
            )

            # Stap 3: Zet discounts toe per product
            logger.info("  Toepassen discounts op producten...")
            matched = 0
            not_matched = 0
            improved = 0
            
            for product in products:
                # Probeer variant regel eerst, anders template regel
                rule = variant_rules.get(product.id)
                if rule is None and product.product_tmpl_id is not None:
                    # Extraheer template ID uit Many2one [id, name] of integer
                    template_id = self._extract_m2o_id(product.product_tmpl_id)
                    if template_id:
                        rule = template_rules.get(template_id)
                
                if rule is None:
                    not_matched += 1
                    continue

                # Bereken sale price van regel
                sale_price = self._compute_sale_price_from_rule(product.list_price, rule)
                if sale_price is None:
                    logger.info(
                        "  %s (id=%s, SKU=%s): sale_price = None (compute_price rule mislukt?)",
                        product.name,
                        product.id,
                        product.default_code,
                    )
                    continue

                current_sale_price = product.sale_price
                try:
                    current_sale_price_value = float(current_sale_price) if current_sale_price is not None else None
                except (TypeError, ValueError):
                    current_sale_price_value = None

                if current_sale_price_value is not None and sale_price >= current_sale_price_value:
                    logger.debug(
                        "  %s (id=%s, SKU=%s): bestaande sale_price %.2f is beter of gelijk",
                        product.name,
                        product.id,
                        product.default_code,
                        current_sale_price_value,
                    )
                    continue

                # Update product als deze pricelist een betere prijs geeft
                product.sale_price = sale_price
                if product.list_price:
                    product.discount_percent = round(
                        (1 - sale_price / product.list_price) * 100, 2
                    )
                
                matched += 1
                if current_sale_price_value is not None:
                    improved += 1
                logger.info(
                    "  %s (id=%s, SKU=%s): €%.2f → €%.2f (%.0f%% off)",
                    product.name,
                    product.id,
                    product.default_code,
                    product.list_price,
                    sale_price,
                    product.discount_percent or 0,
                )

            # Samenvatting
            logger.info("")
            logger.info(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
            logger.info(
                " Pricelist (id=%s) KLAAR",
                pricelist_id,
            )
            logger.info(
                " %s/%s product(en) met sale_price",
                matched,
                len(products),
            )
            if improved > 0:
                logger.info(
                    " %s product(en) kregen een betere prijs door deze pricelist",
                    improved,
                )
            if not_matched > 0:
                logger.info(
                    " %s product(en) zonder regel (gebruiken list_price)",
                    not_matched,
                )
            logger.info(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
            logger.info("")

        except Exception as exc:
            logger.error(
                " FOUT bij toepassen pricelist (id=%s): %s",
                pricelist_id,
                exc,
                exc_info=True,
            )

    def _find_pricelist_id_by_name(self, pricelist_name: str) -> int | None:
        """
        Zoek een pricelist op via naam.
        Dit werkt alleen voor Odoo v19+ (JSON-2 API).
        Voor v14-v18 retourneert dit None.
        
        Args:
            pricelist_name: Exacte of partiële pricelisnamn (case-insensitive)
        
        Returns:
            Pricelist ID of None als niet gevonden
        """
        if not self._is_json2():
            logger.warning(
                "Pricelist '%s' lookup is alleen beschikbaar voor Odoo v19+ (JSON-2 API).",
                pricelist_name,
            )
            return None

        domain = [["name", "=ilike", pricelist_name]]
        fields = ["id", "name"]
        records = self._json2_search_read("product.pricelist", domain, fields, limit=1)

        if not records:
            return None
        
        pricelist_id = int(records[0]["id"])
        logger.debug(
            "Pricelist '%s' gevonden: id=%s",
            pricelist_name,
            pricelist_id,
        )
        return pricelist_id

    def _fetch_pricelist_rules(self, pricelist_id: int) -> list[dict]:
        """
        Haal alle geldige pricelisregels op voor een gegeven pricelist.
        
        Dit werkt alleen voor Odoo v19+ (JSON-2 API).
        Voor v14-v18 retourneert dit een lege lijst.
        
        Regels worden gefilterd op server-side met Odoo domain logica:
        - Scope: alleen product-specifieke regels ("0_product_variant" of "1_product")
        - Datum: alleen actieve regels op vandaag
        
        Args:
            pricelist_id: ID van de pricelist
        
        Returns:
            Lijst met product.pricelist.item records
        """
        if not self._is_json2():
            logger.warning(
                "Pricelist rules ophalen is alleen beschikbaar voor Odoo v19+ (JSON-2 API)."
            )
            return []

        # Vandaag's datum voor date filtering
        today = datetime.now().strftime("%Y-%m-%d")
        
        # Domain met server-side date filtering:
        # date_start moet empty zijn OF kleiner/gelijk aan vandaag
        # date_end moet empty zijn OF groter/gelijk aan vandaag
        domain = [
            ["pricelist_id", "=", pricelist_id],
            ["applied_on", "in", ["0_product_variant", "1_product"]],
            "|", ["date_start", "=", False], ["date_start", "<=", today],
            "|", ["date_end", "=", False], ["date_end", ">=", today],
        ]
        
        fields = [
            "id",
            "applied_on",
            "product_id",
            "product_tmpl_id",
            "compute_price",
            "percent_price",
            "fixed_price",
            "price_discount",
            "date_start",
            "date_end",
        ]

        logger.debug(
            "Ophalen pricelist items voor pricelist_id=%s met server-side date filtering",
            pricelist_id,
        )
        logger.debug("Domain query: %s", domain)
        
        rules = self._json2_search_read("product.pricelist.item", domain, fields, limit=5000)

        logger.info(
            "Pricelist items opgehaald: %s actieve regel(s)",
            len(rules),
        )
        return rules

    @staticmethod
    def _extract_m2o_id(value: Any) -> int | None:
        if isinstance(value, (list, tuple)) and value:
            try:
                return int(value[0])
            except (TypeError, ValueError):
                return None
        if isinstance(value, int):
            return value
        return None

    @staticmethod
    def _compute_sale_price_from_rule(list_price: float, rule: dict) -> float | None:
        """
        Bereken de prijs van een pricelist regel.
        
        Ondersteunde compute_price types:
        - "fixed":      vaste prijs
        - "percentage": percentage korting op list_price
        - "formula":    korting in procent
        
        Args:
            list_price: Standaard verkoopprijs van het product
            rule: Pricelist item regel dict
        
        Returns:
            Berekende sale_price (float) of None als niet berekend kan worden
        """
        if list_price <= 0:
            logger.debug("Kan sale_price niet berekenen: list_price <= 0")
            return None

        compute_price = str(rule.get("compute_price") or "").strip().lower()

        # Type 1: Vaste prijs
        if compute_price == "fixed":
            try:
                fixed = float(rule.get("fixed_price") or 0)
                if 0 < fixed < list_price:
                    return round(fixed, 2)
            except (TypeError, ValueError):
                logger.debug("Kan fixed price niet parseren: %s", rule.get("fixed_price"))
                return None
            return None

        # Type 2: Percentage korting
        if compute_price == "percentage":
            try:
                percent = float(rule.get("percent_price") or 0)
                if 0 < percent < 100:
                    calculated = round(list_price * (1 - percent / 100), 2)
                    logger.debug(
                        "Percentage rule: %.2f - %.0f%% = %.2f",
                        list_price,
                        percent,
                        calculated,
                    )
                    return calculated
            except (TypeError, ValueError):
                logger.debug("Kan percent_price niet parseren: %s", rule.get("percent_price"))
                return None
            return None

        # Type 3: Formula (korting als percentage of waarde)
        if compute_price == "formula":
            try:
                raw_discount = float(rule.get("price_discount") or 0)
            except (TypeError, ValueError):
                logger.debug("Kan price_discount niet parseren: %s", rule.get("price_discount"))
                return None

            # Zet om naar percentage (als waarde tussen 0-1, vermenigvuldig met 100)
            if abs(raw_discount) <= 1:
                percent = abs(raw_discount) * 100
            else:
                percent = abs(raw_discount)

            if 0 < percent < 100:
                calculated = round(list_price * (1 - percent / 100), 2)
                logger.debug(
                    "Formula rule: %.2f - %.0f%% = %.2f",
                    list_price,
                    percent,
                    calculated,
                )
                return calculated

        logger.debug(
            "Onbekend compute_price type '%s' in regel – kan sale_price niet berekenen",
            compute_price,
        )
        return None

    # COMPANY CONFIGURATION (Per-company sync settings)
    def get_company_woo_sync_config(self, company_id: int | None = None) -> list:
        """
        Haal de WooCommerce-synchronisatieconfiguratie op voor één of alle bedrijven.
        
        Args:
            company_id: Specific company ID, or None to fetch all enabled companies
        
        Returns:
            List of OdooCompanyResponse objects with WooCommerce settings
        """
        from .models import OdooCompanyResponse
        
        domain = []
        if company_id:
            domain.append(("id", "=", company_id))
        
        # Full field list requesting all WooCommerce config fields
        full_fields = [
            "id",
            "name",
            "woo_wordpress_plugin_enabled",
            "shopify_plugin_enabled",
            "woo_sync_enabled",
            "woo_sync_interval_mode",
            "woo_sync_interval",
            "woo_product_sync_interval",
            "woo_auto_confirm_paid_orders",
            "woo_auto_confirm_unpaid_orders",
            "woo_create_delivery_picking",
            "woo_track_stock",
            "woo_create_delivery_addresses",
            "woo_url",
            "woo_consumer_key",
            "woo_consumer_secret",
            "woo_last_sync_status",
            "woo_last_error_message",
        ]
        
        # Minimal field list (fallback if custom fields don't exist)
        minimal_fields = ["id", "name"]
        
        try:
            if self._is_json2():
                # Try with woo_sync_enabled filter + full fields
                try:
                    domain_with_filter = domain + [("woo_sync_enabled", "!=", False)]
                    records = self._json2_search_read(
                        "res.company",
                        domain_with_filter,
                        full_fields,
                        limit=1000,
                    )
                except Exception as e:
                    logger.warning(
                        "woo_sync_enabled field not found (custom fields may not be created yet): %s", e
                    )
                    # Fallback: retry without filter and without custom fields
                    try:
                        records = self._json2_search_read(
                            "res.company",
                            domain,
                            minimal_fields,
                            limit=1000,
                        )
                    except Exception as e2:
                        logger.error("Failed to query companies (fallback): %s", e2)
                        records = []
            else:
                # Try classical API with woo_sync_enabled filter + full fields
                try:
                    domain_with_filter = domain + [("woo_sync_enabled", "!=", False)]
                    records = self._call_kw(
                        "res.company",
                        "search_read",
                        [domain_with_filter],
                        {"fields": full_fields},
                    )
                except Exception as e:
                    logger.warning(
                        "woo_sync_enabled field not found (custom fields may not be created yet): %s", e
                    )
                    # Fallback: retry without filter and without custom fields
                    try:
                        records = self._call_kw(
                            "res.company",
                            "search_read",
                            [domain],
                            {"fields": minimal_fields},
                        )
                    except Exception as e2:
                        logger.error("Failed to query companies (fallback): %s", e2)
                        records = []
            
            companies = []
            for record in records:
                try:
                    companies.append(OdooCompanyResponse(**record))
                except Exception as exc:
                    logger.warning(
                        "Company id=%s overgeslagen bij config fetch: %s",
                        record.get("id"),
                        exc,
                    )
            
            logger.info("Opgehaald WooCommerce config voor %s bedrijf(ven)", len(companies))
            return companies
        
        except Exception as exc:
            logger.error("Error fetching company WooCommerce config: %s", exc)
            return []

    def get_all_active_companies(self) -> list:
        """
        Haal alle Odoo-bedrijven op waarvoor WooCommerce-synchronisatie is ingeschakeld.
        Geeft een lijst van OdooCompanyResponse-modellen terug.
        """
        return self.get_company_woo_sync_config(company_id=None)

    def update_company_sync_status(
        self,
        company_id: int,
        status: str,
        message: str | None = None,
    ) -> None:
        """Update company sync status/message so Odoo UI reflects external script progress."""
        vals: dict[str, Any] = {
            "woo_last_sync_status": status,
        }
        if message is not None:
            vals["woo_last_error_message"] = message
        if status == "success":
            vals["woo_last_sync_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if self._is_json2():
            self._json2_call_method(
                "res.company",
                "write",
                ids=[company_id],
                kwargs={"vals": vals},
            )
            return

        self._call_kw("res.company", "write", [[company_id], vals])

    def update_company_sync_progress(
        self,
        company_id: int,
        phase: str,
        percent: float,
        current: int = 0,
        total: int = 0,
        status: str = "syncing",
        message: str | None = None,
        finished: bool = False,
    ) -> None:
        """Update progress fields on res.company so Odoo UI can show live sync progress."""
        progress = max(0.0, min(100.0, float(percent)))
        vals: dict[str, Any] = {
            "odoo_sync_progress_phase": phase,
            "odoo_sync_progress_percent": progress,
            "woo_last_sync_status": status,
        }
        if message is not None:
            vals["woo_last_error_message"] = message
        if status == "success":
            vals["woo_last_sync_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if self._is_json2():
            self._json2_call_method(
                "res.company",
                "write",
                ids=[company_id],
                kwargs={"vals": vals},
            )
            return

        self._call_kw("res.company", "write", [[company_id], vals])

    def get_all_customers(self, company_id: int | None = None) -> list[dict]:
        """
        Haal alle Odoo-klanten op voor klantverificatie en matching.
        Geeft een lijst van klanten terug met id, naam, e-mail, telefoon, btw.

        Wordt gebruikt door de CustomerVerifier om overeenkomsten te vinden tussen WooCommerce- en Odoo-bestellingen.
        """
        if self._is_json2():
            return self._get_all_customers_json2(company_id)
        return self._get_all_customers_jsonrpc(company_id)

    def _get_all_customers_jsonrpc(self, company_id: int | None = None) -> list[dict]:
        """JSON-RPC v14-v18 implementation."""
        domain = [["customer_rank", "=", 1]]  # Only customers, not suppliers
        if company_id:
            domain.append(["company_id", "=", company_id])

        customers: list[dict] = self._call_kw(
            "res.partner",
            "search_read",
            [domain],
            {
                "fields": ["id", "name", "email", "phone", "vat"],
                "limit": 0,  # No limit
            },
        )

        logger.info("Fetched %d Odoo customers for matching", len(customers))
        return customers

    def _get_all_customers_json2(self, company_id: int | None = None) -> list[dict]:
        """REST JSON-2 API implementation."""
        domain = [["customer_rank", "=", 1]]
        if company_id:
            domain.append(["company_id", "=", company_id])

        customers = self._json2_search_read(
            "res.partner",
            domain,
            ["id", "name", "email", "phone", "vat"],
            limit=0,  # No limit
        )

        logger.info("Fetched %d Odoo customers for matching (v19+)", len(customers))
        return customers

    # MULTI-BRANCH B2B SUPPORT (Delivery Addresses)
    def find_delivery_addresses(self, parent_partner_id: int) -> list[dict]:
        """
        Zoek alle leveringsadressen (subcontacten) voor een bovenliggende partner.
        """
        if self._is_json2():
            return self._find_delivery_addresses_json2(parent_partner_id)
        return self._find_delivery_addresses_jsonrpc(parent_partner_id)

    def _find_delivery_addresses_jsonrpc(self, parent_partner_id: int) -> list[dict]:
        """JSON-RPC v14-v18 implementation."""
        domain = [["parent_id", "=", parent_partner_id], ["type", "=", "delivery"]]
        
        delivery_addresses: list[dict] = self._call_kw(
            "res.partner",
            "search_read",
            [domain],
            {"fields": ["id", "name", "city", "street", "zip"], "limit": 0},
        )
        
        return delivery_addresses

    def _find_delivery_addresses_json2(self, parent_partner_id: int) -> list[dict]:
        """REST JSON-2 API implementation."""
        domain = [["parent_id", "=", parent_partner_id], ["type", "=", "delivery"]]
        
        delivery_addresses = self._json2_search_read(
            "res.partner",
            domain,
            ["id", "name", "city", "street", "zip"],
            limit=0,
        )
        
        return delivery_addresses

    def find_delivery_address_by_city(
        self, parent_partner_id: int, city: str
    ) -> dict | None:
        """
        Zoek een leveringsadres voor de bovenliggende partner in een specifieke stad.

        Geeft een leveringsadres-dictionary terug of None indien niet gevonden.
        """
        delivery_addresses = self.find_delivery_addresses(parent_partner_id)
        
        # Look for exact city match (case-insensitive)
        for addr in delivery_addresses:
            if addr.get("city", "").lower() == city.lower():
                logger.info(
                    "Found delivery address for %s in %s: %s",
                    parent_partner_id,
                    city,
                    addr.get("name"),
                )
                return addr
        
        return None

    def find_delivery_address(
        self,
        parent_partner_id: int,
        street: str,
        city: str,
        postcode: str,
    ) -> dict | None:
        """
        Zoek een leveringsadres voor de bovenliggende partner op basis van het volledige adres.

        De matching is niet hoofdlettergevoelig en verwijdert overbodige witruimte.
        """
        delivery_addresses = self.find_delivery_addresses(parent_partner_id)

        target_street = (street or "").strip().lower()
        target_city = (city or "").strip().lower()
        target_zip = (postcode or "").strip().lower()

        for addr in delivery_addresses:
            addr_street = (addr.get("street") or "").strip().lower()
            addr_city = (addr.get("city") or "").strip().lower()
            addr_zip = (addr.get("postcode") or addr.get("zip") or "").strip().lower()
            if addr_street == target_street and addr_city == target_city and addr_zip == target_zip:
                logger.info(
                    "Found delivery address for parent=%s at %s, %s %s: %s",
                    parent_partner_id,
                    street,
                    postcode,
                    city,
                    addr.get("name"),
                )
                return addr

        return None

    def create_delivery_address(
        self,
        parent_partner_id: int,
        street: str,
        city: str,
        postcode: str,
        country_code: str = "NL",
        address_name: str | None = None,
    ) -> int:
        """
        Maak een leveringsadres (subcontact) aan voor een B2B-moederpartner.
        Dit maakt ondersteuning voor meerdere vestigingen mogelijk door leveringsadressen te koppelen aan een moederbedrijf.
        
        Args:
            parent_partner_id: ID van parent company
            street: Street address
            city: City name
            postcode: Postal code
            country_code: ISO country code
            address_name: Name for this branch
        
        Returns:
            ID van het nieuw aangemaakte leveringsadrescontact
        """
        if self._is_json2():
            return self._create_delivery_address_json2(
                parent_partner_id, street, city, postcode, country_code, address_name
            )
        return self._create_delivery_address_jsonrpc(
            parent_partner_id, street, city, postcode, country_code, address_name
        )

    def _create_delivery_address_jsonrpc(
        self,
        parent_partner_id: int,
        street: str,
        city: str,
        postcode: str,
        country_code: str = "NL",
        address_name: str | None = None,
    ) -> int:
        """JSON-RPC v14-v18 implementation."""
        
        # Get parent partner name for address naming
        parent = self._call_kw(
            "res.partner",
            "search_read",
            [[["id", "=", parent_partner_id]]],
            {"fields": ["name", "country_id"], "limit": 1},
        )
        
        if not parent:
            raise ValueError(f"Parent partner {parent_partner_id} not found in Odoo")
        
        parent_name = parent[0]["name"]
        
        # Generate address name if not provided
        if not address_name:
            address_name = f"{parent_name} - {city}"
        
        # Get country ID
        country_ids: list[int] = self._call_kw(
            "res.country", "search", [[["code", "=", country_code.upper()]]]
        )
        country_id = country_ids[0] if country_ids else None
        
        # Build delivery address values
        address_vals = {
            "name": address_name,
            "parent_id": parent_partner_id,
            "type": "delivery",  # Mark as delivery type
            "street": street,
            "city": city,
            "zip": postcode,
            "country_id": country_id,
            "company_id": parent[0].get("company_id", [1])[0] if parent[0].get("company_id") else 1,
        }
        
        # Create delivery address
        delivery_id: int = self._call_kw("res.partner", "create", [address_vals])
        logger.info(
            "Created delivery address id=%s for parent %s: %s, %s",
            delivery_id,
            parent_partner_id,
            city,
            address_name,
        )
        
        return delivery_id

    def _create_delivery_address_json2(
        self,
        parent_partner_id: int,
        street: str,
        city: str,
        postcode: str,
        country_code: str = "NL",
        address_name: str | None = None,
    ) -> int:
        """REST JSON-2 API implementation."""
        
        # Get parent partner name
        parent = self._json2_search_read(
            "res.partner",
            [["id", "=", parent_partner_id]],
            ["name", "country_id", "company_id"],
            limit=1,
        )
        
        if not parent:
            raise ValueError(f"Parent partner {parent_partner_id} not found in Odoo")
        
        parent_name = parent[0]["name"]
        
        # Generate address name if not provided
        if not address_name:
            address_name = f"{parent_name} - {city}"
        
        # Get country ID
        country_results = self._json2_search(
            "res.country", [["code", "=", country_code.upper()]]
        )
        country_id = country_results[0] if country_results else None
        
        # Build delivery address values
        address_vals = {
            "name": address_name,
            "parent_id": parent_partner_id,
            "type": "delivery",
            "street": street,
            "city": city,
            "zip": postcode,
            "country_id": country_id,
        }
        parent_company = parent[0].get("company_id")
        if parent_company:
            address_vals["company_id"] = parent_company[0]
        
        # Create delivery address
        delivery_id = self._json2_create("res.partner", address_vals)
        logger.info(
            "Created delivery address id=%s for parent %s: %s, %s (v19+)",
            delivery_id,
            parent_partner_id,
            city,
            address_name,
        )
        
        return delivery_id
