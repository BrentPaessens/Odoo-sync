import os
import json
import logging
import subprocess
import sys
import uuid
from datetime import timedelta
from pathlib import Path

from odoo import _, api, fields, models
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)


class ResCompany(models.Model):
    """Extend res.company with platform add-on synchronization settings."""
    
    _inherit = 'res.company'

    _SELECTION_DEFAULTS = {
        "woo_sync_interval_mode": "separate",
        "woo_sync_interval": "15",
        "woo_product_sync_interval": "1440",
        "woo_last_sync_status": "pending",
        "woo_sync_trigger_mode": "manual",
        "woo_sync_trigger_type": "orders",
    }

    woo_wordpress_plugin_enabled = fields.Boolean(
        string="WordPress Plugin",
        default=False,
        help="Activeer WordPress connector settings.",
    )

    shopify_plugin_enabled = fields.Boolean(
        string="Shopify Plugin",
        default=False,
        help="Activeer Shopify connector settings (voor toekomstige uitbreiding).",
    )

    # ════════════════════════════════════════════════════════════════════════════
    # ODOO CONNECTION (used by all connectors)
    # ════════════════════════════════════════════════════════════════════════════

    odoo_url = fields.Char(
        string="Odoo URL",
        default="http://127.0.0.1:8069", # Voor testdoeleinden
        help="Interne Odoo URL gebruikt voor connectie.",
    )

    odoo_db = fields.Char(
        string="Odoo Database",
        help="Database naam Odoo authenticatie.",
    )

    odoo_username = fields.Char(
        string="Odoo Username",
        help="gebruikersnaam voor API authentiecatie. (odoo 14 t/m 18)",
    )

    odoo_password = fields.Char(
        string="Odoo Password",
        help="Wachtwoord voor API authentiecatie. (odoo 14 t/m 18)",
    )

    odoo_api_key = fields.Char(
        string="Odoo API Key",
        help="API key voor Odoo API authentiecatie. (odoo 19+)",
    )


    odoo_sync_progress_percent = fields.Float(
        string="Sync Progress (%)",
        default=0.0,
        readonly=True,
        help="Progress bar voor sync status",
    )

    odoo_sync_progress_phase = fields.Char(
        string="Sync Phase",
        readonly=True,
        help="Huidige fase van de sync",
    )


    woo_sync_pid = fields.Integer(
        string="Sync Process PID",
        readonly=True,
    )

    woo_order_cron_id = fields.Many2one(
        comodel_name="ir.cron",
        string="Order Sync Cron",
        readonly=True,
        copy=False,
    )

    woo_order_cron_active = fields.Boolean(
        string="Order Scheduler Active",
        related="woo_order_cron_id.active",
        readonly=True,
    )

    woo_order_cron_status = fields.Char(
        string="Order Scheduler Status",
        compute="_compute_woo_order_cron_status",
        readonly=True,
    )

    woo_order_cron_nextcall = fields.Datetime(
        string="Order Scheduler Next Run",
        related="woo_order_cron_id.nextcall",
        readonly=True,
    )

    woo_product_cron_id = fields.Many2one(
        comodel_name="ir.cron",
        string="Product Sync Cron",
        readonly=True,
        copy=False,
    )

    woo_product_cron_status = fields.Char(
        string="Product Scheduler Status",
        compute="_compute_woo_product_cron_status",
        readonly=True,
    )

    woo_product_cron_nextcall = fields.Datetime(
        string="Product Scheduler Next Run",
        related="woo_product_cron_id.nextcall",
        readonly=True,
    )

    @api.depends("woo_order_cron_id", "woo_order_cron_id.active")
    def _compute_woo_order_cron_status(self):
        """Show a human-readable scheduler state instead of a non-clickable checkbox."""
        for company in self:
            if not company.woo_order_cron_id:
                company.woo_order_cron_status = _("Not configured")
            elif company.woo_order_cron_id.active:
                company.woo_order_cron_status = _("Active")
            else:
                company.woo_order_cron_status = _("Inactive")

    @api.depends("woo_product_cron_id", "woo_product_cron_id.active")
    def _compute_woo_product_cron_status(self):
        """Show scheduler state for automatic product synchronization."""
        for company in self:
            if not company.woo_product_cron_id:
                company.woo_product_cron_status = _("Not configured")
            elif company.woo_product_cron_id.active:
                company.woo_product_cron_status = _("Active")
            else:
                company.woo_product_cron_status = _("Inactive")
    
    # ════════════════════════════════════════════════════════════════════════════
    # SYNC ENABLEMENT & SCHEDULING
    # ════════════════════════════════════════════════════════════════════════════
    
    woo_sync_enabled = fields.Boolean(
        string="Enable Platform Sync",
        default=False,
        help="Enable automatic order and product synchronization for enabled commerce connectors"
    )

    woo_sync_interval_mode = fields.Selection(
        selection=[
            ('shared', 'Shared interval'),
            ('separate', 'Separate intervals'),
        ],
        default='separate',
        string="Sync Interval Mode",
        help="Use one interval for both orders and products, or configure them separately.",
    )
    
    woo_sync_interval = fields.Selection(
        selection=[
            ('15', 'Every 15 minutes'),
            ('30', 'Every 30 minutes'),
            ('60', 'Every 1 hour'),
            ('360', 'Every 6 hours'),
            ('1440', 'Once per day'),
            ('manual', 'Manual only'),
        ],
        default='15',
        string="Order Sync Interval",
        help="How often to check for new connector orders"
    )

    woo_product_sync_interval = fields.Selection(
        selection=[
            ('1440', 'Once per day'),
            ('10080', 'Once a week'),
            ('20160', 'Once every 2 weeks'),
            ('40320', 'Once a month'),
            ('manual', 'Manual only'),
        ],
        default='1440',
        string="Product Sync Interval",
        help="How often to sync products when separate intervals are enabled",
    )
    
    # ════════════════════════════════════════════════════════════════════════════
    # ORDER HANDLING
    # ════════════════════════════════════════════════════════════════════════════
    
    woo_auto_confirm_paid_orders = fields.Boolean(
        string="Auto-Confirm Paid Orders",
        default=False,
        help="Automatisch bevestigen van betaalde orders als Sale Order"
    )
    
    woo_auto_confirm_unpaid_orders = fields.Boolean(
        string="Auto-Confirm Unpaid Orders",
        default=False,
        help="Also confirm unpaid/pending orders (usually not recommended)"
    )
    
    woo_create_delivery_addresses = fields.Boolean(
        string="Create Delivery Addresses",
        default=False,
        help="Automatisch aanmaken van leveringsadressen"
    )

    woo_create_delivery_picking = fields.Boolean(
    string="Delivery Picking Flow",
    default=False,
    help="Na bevestiging van een betaalde order: maak automatisch een leverbon aan."
    )
    
    woo_track_stock = fields.Boolean(
        string="Track Internal Stock",
        default=False,
        help="Reserveer interne voorraad bij het aanmaken van de leverbon."
    )
    
    # ════════════════════════════════════════════════════════════════════════════
    # WOOCOMMERCE API CONNECTION
    # ════════════════════════════════════════════════════════════════════════════
    
    woo_url = fields.Char(
        string="WooCommerce Store URL",
        help="URL naar uw WooCommerce winkel. Example: https://shop.example.com"
    )
    
    woo_consumer_key = fields.Char(
        string="WooCommerce Consumer Key",
        help="Get from: WordPress Admin → WooCommerce → Settings → Advanced → REST API"
    )
    
    woo_consumer_secret = fields.Char(
        string="WooCommerce Consumer Secret",
        help="Get from: WordPress Admin → WooCommerce → Settings → Advanced → REST API"
    )

    # ════════════════════════════════════════════════════════════════════════════
    # OPTIONAL: PER-COMPANY OVERRIDES (if using multiple WooCommerce stores)
    # ════════════════════════════════════════════════════════════════════════════
    
    woo_company_id = fields.Char(
        string="WooCommerce Company ID",
        help="(Optional) If this company represents a specific WooCommerce company/branch"
    )
    
    # ════════════════════════════════════════════════════════════════════════════
    # STATUS & LOGGING
    # ════════════════════════════════════════════════════════════════════════════
    
    woo_last_sync_time = fields.Datetime(
        string="Last Sync Time",
        readonly=True,
        help="Timestamp last successful sync"
    )
    
    woo_last_sync_status = fields.Selection(
        selection=[
            ('pending', 'Pending - Never synced'),
            ('syncing', 'Syncing - In progress'),
            ('success', 'Success - Last sync successful'),
            ('failed', 'Failed - Last sync had errors'),
        ],
        default='pending',
        string="Last Sync Status",
        readonly=True
    )
    
    woo_last_error_message = fields.Text(
        string="Last Message",
        readonly=True,
        help="Details of the last sync"
    )

    woo_sync_run_id = fields.Char(
        string="Sync Run ID",
        readonly=True,
        copy=False,
    )

    woo_sync_log_offset = fields.Integer(
        string="Sync Log Offset",
        readonly=True,
        copy=False,
        default=0,
    )

    woo_sync_trigger_mode = fields.Selection(
        selection=[("manual", "Manual"), ("cron", "Cron")],
        string="Sync Trigger Mode",
        readonly=True,
        copy=False,
    )

    woo_sync_trigger_type = fields.Selection(
        selection=[("orders", "Orders"), ("products", "Products")],
        string="Sync Trigger Type",
        readonly=True,
        copy=False,
    )

    @api.model
    def default_get(self, fields_list):
        """Ensure plugin and sync checkboxes are off by default for new companies."""
        values = super().default_get(fields_list)
        values["woo_wordpress_plugin_enabled"] = False
        values["shopify_plugin_enabled"] = False
        values["woo_sync_enabled"] = False
        values["woo_sync_interval_mode"] = 'separate'
        values["woo_product_sync_interval"] = '1440'
        values["woo_auto_confirm_paid_orders"] = False
        values["woo_use_stock_delivery_flow"] = False
        values["woo_create_delivery_addresses"] = False
        return values

    @api.model_create_multi
    def create(self, vals_list):
        """Keep plugin and sync toggles off when callers do not explicitly provide values."""
        for vals in vals_list:
            vals.setdefault("woo_wordpress_plugin_enabled", False)
            vals.setdefault("shopify_plugin_enabled", False)
            vals.setdefault("woo_sync_enabled", False)
            vals.setdefault("woo_sync_interval_mode", 'separate')
            vals.setdefault("woo_product_sync_interval", '1440')
            vals.setdefault("woo_auto_confirm_paid_orders", False)
            vals.setdefault("woo_use_stock_delivery_flow", False)
            vals.setdefault("woo_create_delivery_addresses", False)
        companies = super().create(vals_list)
        companies._sync_scheduler_cron_state()
        return companies

    def write(self, vals):
        """Keep order cron in sync whenever relevant company settings change."""
        vals = self._sanitize_selection_vals(vals)
        previous_state = {
            company.id: {
                "status": company.woo_last_sync_status,
                "run_id": company.woo_sync_run_id,
                "sync_type": company.woo_sync_trigger_type,
                "mode": company.woo_sync_trigger_mode,
            }
            for company in self
        }

        result = super().write(vals)
        if self.env.context.get("skip_scheduler_cron_sync") or self.env.context.get("skip_order_cron_sync"):
            return result

        relevant_fields = {
            "name",
            "woo_wordpress_plugin_enabled",
            "woo_sync_enabled",
            "woo_sync_interval_mode",
            "woo_sync_interval",
            "woo_product_sync_interval",
        }
        if relevant_fields.intersection(vals):
            self._sync_scheduler_cron_state()

        if not self.env.context.get("skip_sync_logging_hooks"):
            sync_progress_fields = {
                "woo_last_sync_status",
                "woo_last_error_message",
                "odoo_sync_progress_phase",
                "odoo_sync_progress_percent",
                "woo_sync_pid",
            }
            if sync_progress_fields.intersection(vals):
                for company in self:
                    company._ingest_woo_sync_item_errors()
                    old_status = previous_state.get(company.id, {}).get("status")
                    if old_status == "syncing" and company.woo_last_sync_status in ("success", "failed"):
                        summary = company.woo_last_error_message or ""
                        company._log_woo_sync_event(
                            event="run_end",
                            level="INFO" if company.woo_last_sync_status == "success" else "ERROR",
                            sync_type=company.woo_sync_trigger_type or previous_state.get(company.id, {}).get("sync_type"),
                            trigger_mode=company.woo_sync_trigger_mode or previous_state.get(company.id, {}).get("mode"),
                            run_id=company.woo_sync_run_id or previous_state.get(company.id, {}).get("run_id"),
                            details=f"status={company.woo_last_sync_status}; summary={summary}",
                        )
                        company.with_context(skip_sync_logging_hooks=True).write(
                            {
                                "woo_sync_run_id": False,
                                "woo_sync_log_offset": 0,
                                "woo_sync_trigger_mode": False,
                                "woo_sync_trigger_type": False,
                            }
                        )
        return result

    def read(self, fields=None, load="_classic_read"):
        """Coerce invalid selection values to defaults to keep the UI stable."""
        records = super().read(fields=fields, load=load)
        if not records:
            return records

        selection_fields = self._SELECTION_DEFAULTS.keys()
        for row in records:
            for field_name in selection_fields:
                if fields is not None and field_name not in fields:
                    continue
                value = row.get(field_name)
                sanitized = self._sanitize_selection_value(field_name, value)
                if sanitized != value:
                    row[field_name] = sanitized
        return records

    @api.model
    def _sanitize_selection_value(self, field_name, value):
        if value in (None, False, ""):
            return value
        field = self._fields.get(field_name)
        selection = getattr(field, "selection", None)
        if not selection:
            return value
        valid = {key for key, _ in selection}
        if value in valid:
            return value
        return self._SELECTION_DEFAULTS.get(field_name, False)

    @api.model
    def _sanitize_selection_vals(self, vals):
        if not vals:
            return vals
        sanitized = dict(vals)
        for field_name in self._SELECTION_DEFAULTS.keys():
            if field_name in sanitized:
                sanitized[field_name] = self._sanitize_selection_value(
                    field_name,
                    sanitized.get(field_name),
                )
        return sanitized

    @staticmethod
    def _woo_sync_log_path(company_id):
        """Return dedicated sync logfile path per company."""
        return Path("/tmp") / f"woo_sync_company_{company_id}.log"

    def _log_woo_sync_event(self, event, level="INFO", sync_type=None, trigger_mode=None, run_id=None, details=""):
        """Write structured Woo sync events to Odoo ir.logging and server logger."""
        self.ensure_one()
        level_value = (level or "INFO").upper()
        sync_type_value = sync_type or "unknown"
        message = (
            f"event={event}, company_id={self.id}, company={self.display_name}, "
            f"sync_type={sync_type_value}, details={details or '-'}"
        )

        if sync_type_value == "orders":
            log_name = "orders sync"
        elif sync_type_value == "products":
            log_name = "products sync"
        else:
            log_name = "woo sync"

        if level_value == "ERROR":
            _logger.error(message)
        elif level_value == "WARNING":
            _logger.warning(message)
        else:
            _logger.info(message)

        try:
            self.env["ir.logging"].sudo().create(
                {
                    "name": log_name,
                    "type": "server",
                    "dbname": self.env.cr.dbname,
                    "level": level_value,
                    "message": message,
                    "path": "odoo_woocommerce_sync.models.res_company",
                    "func": "_log_woo_sync_event",
                    "line": 0,
                }
            )
        except Exception:
            _logger.exception("Could not persist Woo sync event into ir.logging")

    def _ingest_woo_sync_item_errors(self):
        """Ingest structured per-item errors from the Woo sync logfile into ir.logging."""
        self.ensure_one()
        run_id = (self.woo_sync_run_id or "").strip()
        if not run_id:
            return

        log_path = self._woo_sync_log_path(self.id)
        if not log_path.exists():
            return

        previous_offset = max(int(self.woo_sync_log_offset or 0), 0)
        file_size = log_path.stat().st_size
        if previous_offset > file_size:
            previous_offset = 0

        next_offset = previous_offset
        marker = "SYNC_EVENT|"
        with log_path.open("r", encoding="utf-8", errors="replace") as stream:
            stream.seek(previous_offset)
            for line in stream:
                if marker not in line:
                    continue
                raw_payload = line.split(marker, 1)[1].strip()
                try:
                    payload = json.loads(raw_payload)
                except json.JSONDecodeError:
                    continue

                if payload.get("run_id") != run_id:
                    continue
                if payload.get("event") != "item_error":
                    continue

                sync_type = payload.get("sync_type") or self.woo_sync_trigger_type
                item_type = payload.get("item_type") or "item"
                item_ref = payload.get("item_ref") or "unknown"
                error_message = payload.get("error") or payload.get("message") or "unknown error"
                self._log_woo_sync_event(
                    event="item_error",
                    level="ERROR",
                    sync_type=sync_type,
                    trigger_mode=self.woo_sync_trigger_mode,
                    run_id=run_id,
                    details=f"{item_type}={item_ref}; error={error_message}",
                )

            next_offset = stream.tell()

        if next_offset != previous_offset:
            self.with_context(skip_sync_logging_hooks=True).write({"woo_sync_log_offset": next_offset})

    def _get_sync_profile(self):
        """Collect current company sync settings from form fields."""
        self.ensure_one()
        return {
            "woo_url": (self.woo_url or "").strip(),
            "woo_consumer_key": (self.woo_consumer_key or "").strip(),
            "woo_consumer_secret": (self.woo_consumer_secret or "").strip(),
        }

    def _get_odoo_profile(self):
        """Collect Odoo connection settings from the company form."""
        self.ensure_one()
        return {
            "odoo_url": (self.odoo_url or "").strip(),
            "odoo_db": (self.odoo_db or "").strip(),
            "odoo_username": (self.odoo_username or "").strip(),
            "odoo_password": self.odoo_password or "",
            "odoo_api_key": (self.odoo_api_key or "").strip(),
        }

    def _get_order_interval_minutes(self):
        """Return configured order sync interval in minutes, or None when manual mode is active."""
        self.ensure_one()
        value = (self.woo_sync_interval or "manual").strip()
        if value == "manual":
            return None
        try:
            interval = int(value)
        except ValueError:
            return None
        return interval if interval > 0 else None

    def _get_product_interval_minutes(self):
        """Return configured product sync interval in minutes for current mode."""
        self.ensure_one()
        raw_value = self.woo_product_sync_interval if self.woo_sync_interval_mode == "separate" else self.woo_sync_interval
        value = (raw_value or "manual").strip()
        if value == "manual":
            return None
        try:
            interval = int(value)
        except ValueError:
            return None
        return interval if interval > 0 else None

    def _should_enable_order_cron(self):
        """Determine whether automatic order scheduling should be active for this company."""
        self.ensure_one()
        return bool(
            self.woo_wordpress_plugin_enabled
            and self.woo_sync_enabled
            and self._get_order_interval_minutes()
        )

    def _should_enable_product_cron(self):
        """Determine whether automatic product scheduling should be active for this company."""
        self.ensure_one()
        return bool(
            self.woo_wordpress_plugin_enabled
            and self.woo_sync_enabled
            and self._get_product_interval_minutes()
        )

    def _prepare_order_cron_vals(self):
        """Build cron values for this company-specific automatic order sync."""
        self.ensure_one()
        model_id = self.env["ir.model"]._get_id("res.company")
        cron_user = self.env.ref("base.user_admin", raise_if_not_found=False) or self.env.user
        interval_minutes = self._get_order_interval_minutes() or 15
        return {
            "name": f"Woo Order Sync - {self.display_name} (Company {self.id})",
            "model_id": model_id,
            "state": "code",
            "code": f"model.browse({self.id})._cron_run_order_sync()",
            "interval_number": interval_minutes,
            "interval_type": "minutes",
            "numbercall": -1,
            "doall": False,
            "active": self._should_enable_order_cron(),
            "user_id": cron_user.id,
        }

    def _prepare_product_cron_vals(self):
        """Build cron values for this company-specific automatic product sync."""
        self.ensure_one()
        model_id = self.env["ir.model"]._get_id("res.company")
        cron_user = self.env.ref("base.user_admin", raise_if_not_found=False) or self.env.user
        interval_minutes = self._get_product_interval_minutes() or 1440
        values = {
            "name": f"Woo Product Sync - {self.display_name} (Company {self.id})",
            "model_id": model_id,
            "state": "code",
            "code": f"model.browse({self.id})._cron_run_product_sync()",
            "interval_number": interval_minutes,
            "interval_type": "minutes",
            "numbercall": -1,
            "doall": False,
            "active": self._should_enable_product_cron(),
            "user_id": cron_user.id,
        }
        # In shared mode, offset product scheduler a bit to avoid colliding with order cron.
        if self.woo_sync_interval_mode == "shared":
            values["nextcall"] = fields.Datetime.now() + timedelta(minutes=1)
        return values

    def _sanitize_cron_values(self, values):
        """Keep only cron fields that exist in the current Odoo version."""
        available_fields = set(self.env["ir.cron"]._fields)
        return {key: value for key, value in values.items() if key in available_fields}

    def _disable_order_cron(self):
        """Disable existing order cron while preserving the technical record."""
        self.ensure_one()
        cron = self.woo_order_cron_id.sudo()
        if cron:
            cron.write({"active": False})

    def _disable_product_cron(self):
        """Disable existing product cron while preserving the technical record."""
        self.ensure_one()
        cron = self.woo_product_cron_id.sudo()
        if cron:
            cron.write({"active": False})

    def _ensure_order_cron(self):
        """Create or update the company-specific order cron according to current settings."""
        self.ensure_one()
        if not self._should_enable_order_cron():
            self._disable_order_cron()
            return

        cron_values = self._sanitize_cron_values(self._prepare_order_cron_vals())
        cron = self.woo_order_cron_id.sudo()
        if cron:
            cron.write(cron_values)
            return

        cron = self.env["ir.cron"].sudo().create(cron_values)
        self.with_context(skip_scheduler_cron_sync=True, skip_order_cron_sync=True).sudo().write({"woo_order_cron_id": cron.id})

    def _ensure_product_cron(self):
        """Create or update the company-specific product cron according to current settings."""
        self.ensure_one()
        if not self._should_enable_product_cron():
            self._disable_product_cron()
            return

        cron_values = self._sanitize_cron_values(self._prepare_product_cron_vals())
        cron = self.woo_product_cron_id.sudo()
        if cron:
            cron.write(cron_values)
            return

        cron = self.env["ir.cron"].sudo().create(cron_values)
        self.with_context(skip_scheduler_cron_sync=True, skip_order_cron_sync=True).sudo().write({"woo_product_cron_id": cron.id})

    def _sync_scheduler_cron_state(self):
        """Synchronize automatic order/product cron state for all companies."""
        for company in self:
            company._ensure_order_cron()
            company._ensure_product_cron()

    def _sync_order_cron_state(self):
        """Backward-compatible alias to synchronize scheduler state."""
        self._sync_scheduler_cron_state()

    def action_rebuild_sync_schedulers(self):
        """Manual action to (re)build scheduler records from current settings."""
        self.ensure_one()
        self._sync_scheduler_cron_state()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Platform Add-ons"),
                "message": _("Order/Product schedulers zijn bijgewerkt op basis van je huidige instellingen."),
                "type": "success",
                "sticky": False,
            },
        }

    def _cron_run_order_sync(self):
        """Cron-safe wrapper that triggers order sync only when the company is ready."""
        self.ensure_one()
        if not self._should_enable_order_cron():
            return

        self._log_woo_sync_event(
            event="cron_start",
            level="INFO",
            sync_type="orders",
            trigger_mode="cron",
            details="order cron job started",
        )

        if self.woo_last_sync_status == "syncing":
            self._release_stale_sync_lock_if_needed(timeout_seconds=10)
        if self.woo_last_sync_status == "syncing":
            _logger.info("Order cron skipped for company %s because a sync is already running.", self.id)
            self._log_woo_sync_event(
                event="cron_end",
                level="WARNING",
                sync_type="orders",
                trigger_mode="cron",
                details="skipped: sync already running",
            )
            return

        try:
            self.with_context(woo_sync_trigger_mode="cron").action_sync_orders()
            self._log_woo_sync_event(
                event="cron_end",
                level="INFO",
                sync_type="orders",
                trigger_mode="cron",
                details="order cron finished: worker queued",
            )
        except UserError as exc:
            _logger.warning("Order cron skipped for company %s: %s", self.id, exc)
            self._log_woo_sync_event(
                event="cron_end",
                level="WARNING",
                sync_type="orders",
                trigger_mode="cron",
                details=f"user error: {exc}",
            )
        except Exception:
            _logger.exception("Unexpected error while running order cron for company %s", self.id)
            self._log_woo_sync_event(
                event="cron_end",
                level="ERROR",
                sync_type="orders",
                trigger_mode="cron",
                details="unexpected error while triggering order sync",
            )

    def _cron_run_product_sync(self):
        """Cron-safe wrapper that triggers product sync only when the company is ready."""
        self.ensure_one()
        if not self._should_enable_product_cron():
            return

        self._log_woo_sync_event(
            event="cron_start",
            level="INFO",
            sync_type="products",
            trigger_mode="cron",
            details="product cron job started",
        )

        if self.woo_last_sync_status == "syncing":
            self._release_stale_sync_lock_if_needed(timeout_seconds=10)
        if self.woo_last_sync_status == "syncing":
            _logger.info("Product cron skipped for company %s because a sync is already running.", self.id)
            self._log_woo_sync_event(
                event="cron_end",
                level="WARNING",
                sync_type="products",
                trigger_mode="cron",
                details="skipped: sync already running",
            )
            return

        try:
            self.with_context(woo_sync_trigger_mode="cron").action_sync_products()
            self._log_woo_sync_event(
                event="cron_end",
                level="INFO",
                sync_type="products",
                trigger_mode="cron",
                details="product cron finished: worker queued",
            )
        except UserError as exc:
            _logger.warning("Product cron skipped for company %s: %s", self.id, exc)
            self._log_woo_sync_event(
                event="cron_end",
                level="WARNING",
                sync_type="products",
                trigger_mode="cron",
                details=f"user error: {exc}",
            )
        except Exception:
            _logger.exception("Unexpected error while running product cron for company %s", self.id)
            self._log_woo_sync_event(
                event="cron_end",
                level="ERROR",
                sync_type="products",
                trigger_mode="cron",
                details="unexpected error while triggering product sync",
            )

    def action_test_woo_connection(self):
        """Validate required WooCommerce fields and show a success notification."""
        self.ensure_one()

        profile = self._get_sync_profile()

        if not profile["woo_url"]:
            raise UserError(_("WooCommerce Store URL ontbreekt."))
        if not profile["woo_consumer_key"]:
            raise UserError(_("WooCommerce Consumer Key ontbreekt."))
        if not profile["woo_consumer_secret"]:
            raise UserError(_("WooCommerce Consumer Secret ontbreekt."))

        self.write(
            {
                "woo_last_sync_status": "success",
                "woo_last_error_message": _("✓ Verbindingsgegevens zijn compleet en geldig."),
            }
        )

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Platform Add-ons"),
                "message": _("✓ Verbindingstest geslaagd! Alle API-gegevens zijn ingevuld."),
                "type": "success",
                "sticky": False,
            },
        }

    def _find_woocommerce_script(self):
        """Find WooCommerce main.py script in multiple possible locations."""
        import os
        
        # Possible paths to search (in order of preference)
        possible_paths = []
        
        # 1. Docker mount path (primary - when running in Odoo container)
        possible_paths.append(Path("/mnt/extra-addons/WooCommerce/main.py"))
        
        # 2. Relative to this module (development/local case)
        repo_root = Path(__file__).resolve().parents[2]
        possible_paths.append(repo_root / "WooCommerce" / "main.py")
        
        # 3. Check environment variable for custom path
        env_path = os.environ.get("VERVIO_SYNC_SCRIPT")
        if env_path:
            possible_paths.append(Path(env_path))
        
        # 4. Other common Docker paths
        possible_paths.extend([
            Path("/app/WooCommerce/main.py"),
            Path("/opt/odoo/WooCommerce/main.py"),
            Path("/home/odoo/WooCommerce/main.py"),
        ])
        
        for script_path in possible_paths:
            if script_path.exists():
                return script_path
        
        # If not found, provide helpful error message
        error_msg = (
            f"Sync-script niet gevonden. Gezochte locaties:\n"
            + "\n".join(str(p) for p in possible_paths)
        )
        raise UserError(_(error_msg))

    def _build_sync_command(self, main_script, sync_flag):
        """Build command for product or order sync with explicit company context."""
        self.ensure_one()
        command = [sys.executable or "python", str(main_script), sync_flag]
        if sync_flag == "--products":
            command.extend(["--company", str(self.id)])
        elif sync_flag == "--orders":
            command.extend(["--once", "--company", str(self.id)])
        return command

    def _mark_sync_failed(self, message, phase=None):
        """Mark company sync as failed and release any sync lock."""
        self.ensure_one()
        self.write(
            {
                "woo_last_sync_status": "failed",
                "woo_last_error_message": message,
                "odoo_sync_progress_phase": phase or _("Sync mislukt"),
                "woo_sync_pid": False,
            }
        )

    @staticmethod
    def _is_pid_alive(pid):
        """Return True if the PID exists in the current OS namespace."""
        if not pid:
            return False
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False

    def _release_stale_sync_lock_if_needed(self, timeout_seconds=10):
        """Auto-release stuck sync lock when worker process is dead."""
        self.ensure_one()
        if self.woo_last_sync_status != "syncing":
            return False

        pid = int(self.woo_sync_pid or 0)
        if pid and not self._is_pid_alive(pid):
            self._mark_sync_failed(
                _("❌ Sync worker is onverwacht gestopt (PID %s).") % pid,
                phase=_("Sync worker gestopt"),
            )
            return True

        return False

    def action_mark_stale_sync_failed(self, timeout_seconds=10):
        """Called by UI watchdog to fail stuck sync and unlock new starts."""
        self.ensure_one()
        return self._release_stale_sync_lock_if_needed(timeout_seconds=int(timeout_seconds or 10))

    def action_stop_sync(self):
        """Force-stop current sync process (if alive) and mark as failed/unlocked."""
        self.ensure_one()

        pid = int(self.woo_sync_pid or 0)
        if self.woo_last_sync_status != "syncing":
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Platform Add-ons"),
                    "message": _("Er draait momenteel geen actieve sync."),
                    "type": "warning",
                    "sticky": False,
                },
            }

        if pid and self._is_pid_alive(pid):
            try:
                os.kill(pid, 15)
            except OSError as exc:
                raise UserError(_("Kon sync-proces niet stoppen (PID %s): %s") % (pid, exc)) from exc

        self._mark_sync_failed(
            _("Sync manueel gestopt door gebruiker."),
            phase=_("Manueel gestopt"),
        )

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Platform Add-ons"),
                "message": _("Sync is gestopt. Je kan nu opnieuw starten."),
                "type": "danger",
                "sticky": False,
            },
        }

    def _run_woocommerce_cli_sync(self, sync_flag, success_label, sync_type, trigger_mode="manual"):
        """Start WooCommerce CLI sync in background and return immediate status notification."""
        self.ensure_one()
        profile = self._get_sync_profile()
        odoo_profile = self._get_odoo_profile()

        if not self.woo_wordpress_plugin_enabled:
            raise UserError(_("Activeer eerst de WooCommerce/WordPress connector."))

        if not profile["woo_url"]:
            raise UserError(_("WooCommerce Store URL ontbreekt."))
        if self.woo_last_sync_status == "syncing":
            self._release_stale_sync_lock_if_needed(timeout_seconds=10)
        if self.woo_last_sync_status == "syncing":
            raise UserError(
                _("Er draait al een sync voor dit bedrijf. Stop de sync of wacht tot deze klaar is.")
            )
        if not odoo_profile["odoo_url"]:
            raise UserError(_("Odoo URL ontbreekt. Vul de Odoo Connection-velden in."))
        if not odoo_profile["odoo_db"]:
            raise UserError(_("Odoo Database ontbreekt. Vul de Odoo Connection-velden in."))
        # Credentials may come from company fields OR from WooCommerce/.env.
        # Do not hard-fail here when fields are empty; the sync runner validates final auth config.

        main_script = self._find_woocommerce_script()
        woo_dir = main_script.parent

        command = self._build_sync_command(main_script, sync_flag)
        internal_odoo_url = os.environ.get("ODOO_INTERNAL_URL", "http://127.0.0.1:8069")
        env = os.environ.copy()
        run_id = str(uuid.uuid4())
        env["ODOO_URL"] = internal_odoo_url
        env["ODOO_DB"] = odoo_profile["odoo_db"]
        env["WOO_SYNC_RUN_ID"] = run_id
        env["WOO_SYNC_TYPE"] = sync_type
        env["WOO_SYNC_TRIGGER_MODE"] = trigger_mode
        if odoo_profile["odoo_username"]:
            env["ODOO_USERNAME"] = odoo_profile["odoo_username"]
        if odoo_profile["odoo_password"]:
            env["ODOO_PASSWORD"] = odoo_profile["odoo_password"]
        if odoo_profile["odoo_api_key"]:
            env["ODOO_API_KEY"] = odoo_profile["odoo_api_key"]

        log_file = self._woo_sync_log_path(self.id)
        log_offset_start = log_file.stat().st_size if log_file.exists() else 0

        self._log_woo_sync_event(
            event="manual_start" if trigger_mode == "manual" else "cron_trigger",
            level="INFO",
            sync_type=sync_type,
            trigger_mode=trigger_mode,
            run_id=run_id,
            details=f"requested_by={self.env.user.login}; label={success_label}",
        )

        # Mark as syncing - user can see progress in Last Sync Status.
        self.write(
            {
                "woo_last_sync_status": "syncing",
                "woo_last_error_message": _(
                    " %s gestart. Voortgang wordt live bijgewerkt in deze pagina."
                )
                % success_label,
                "odoo_sync_progress_percent": 0.0,
                "odoo_sync_progress_phase": _("Wachten op sync worker..."),
                "woo_sync_run_id": run_id,
                "woo_sync_log_offset": log_offset_start,
                "woo_sync_trigger_mode": trigger_mode,
                "woo_sync_trigger_type": sync_type,
            }
        )

        try:
            with log_file.open("a", encoding="utf-8") as stream:
                process = subprocess.Popen(
                    command,
                    cwd=str(woo_dir),
                    env=env,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            self.write({"woo_sync_pid": process.pid})
            self._log_woo_sync_event(
                event="worker_start",
                level="INFO",
                sync_type=sync_type,
                trigger_mode=trigger_mode,
                run_id=run_id,
                details=f"pid={process.pid}; logfile={log_file}",
            )
        except Exception as exc:
            error_msg = str(exc)
            self.write(
                {
                    "woo_last_sync_status": "failed",
                    "woo_last_error_message": _(" Fout: %s") % error_msg,
                    "odoo_sync_progress_phase": _("Starten mislukt"),
                    "woo_sync_pid": False,
                    "woo_sync_run_id": False,
                    "woo_sync_log_offset": 0,
                    "woo_sync_trigger_mode": False,
                    "woo_sync_trigger_type": False,
                }
            )
            self._log_woo_sync_event(
                event="run_end",
                level="ERROR",
                sync_type=sync_type,
                trigger_mode=trigger_mode,
                run_id=run_id,
                details=f"failed to spawn worker: {error_msg}",
            )
            raise UserError(_("Fout bij starten van sync: %s") % exc) from exc

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Platform Add-ons"),
                "message": _(
                    "%s gestart. Controleer de progress bar en statusvelden tot de sync klaar is."
                )
                % success_label,
                "type": "info",
                "sticky": False,
            },
        }

    def action_sync_products(self):
        """Start product sync immediately on the server when user clicks the button."""
        self.ensure_one()
        trigger_mode = self.env.context.get("woo_sync_trigger_mode", "manual")
        return self._run_woocommerce_cli_sync(
            "--products",
            "Product Sync",
            sync_type="products",
            trigger_mode=trigger_mode,
        )

    def action_sync_orders(self):
        """Start order sync immediately on the server when user clicks the button."""
        self.ensure_one()
        trigger_mode = self.env.context.get("woo_sync_trigger_mode", "manual")
        return self._run_woocommerce_cli_sync(
            "--orders",
            "Order Sync",
            sync_type="orders",
            trigger_mode=trigger_mode,
        )
