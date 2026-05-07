from odoo import _, models
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def _get_manual_sync_company(self):
        companies = self.mapped("company_id").filtered(lambda company: company)
        if len(companies) > 1:
            raise UserError(_("Selecteer records van één bedrijf tegelijk."))
        if companies:
            return companies[0]

        company = self.env.company
        if not company:
            raise UserError(_("Geen actief bedrijf gevonden."))
        return company

    def action_sync_platform_orders(self):
        """Trigger order sync from sale.order and return lightweight UI payload."""
        company = self._get_manual_sync_company()
        company.sudo().action_sync_orders()
        return {
            "status": "started",
            "company_id": company.id,
        }

    def action_get_platform_order_sync_status(self, company_id):
        """Return current order sync status for sale.order polling UI."""
        company = self.env["res.company"].browse(int(company_id)).exists()
        if not company:
            raise UserError(_("Bedrijf niet gevonden voor order sync status."))
        if company not in self.env.companies:
            raise UserError(_("Je hebt geen toegang tot dit bedrijf."))

        company = company.sudo()
        company._release_stale_sync_lock_if_needed(timeout_seconds=10)
        return {
            "status": company.woo_last_sync_status or "pending",
            "phase": company.odoo_sync_progress_phase or "",
            "error_message": company.woo_last_error_message or "",
        }