from odoo import _, models
from odoo.exceptions import UserError


class ProductTemplate(models.Model):
    _inherit = "product.template"

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

    def action_sync_platform_products(self):
        """Trigger product sync from product.template and return lightweight UI payload."""
        company = self._get_manual_sync_company()
        company.sudo().action_sync_products()
        return {
            "status": "started",
            "company_id": company.id,
        }

    def action_get_platform_product_sync_status(self, company_id):
        """Return current product sync status for product.template polling UI."""
        company = self.env["res.company"].browse(int(company_id)).exists()
        if not company:
            raise UserError(_("Bedrijf niet gevonden voor product sync status."))
        if company not in self.env.companies:
            raise UserError(_("Je hebt geen toegang tot dit bedrijf."))

        company = company.sudo()
        company._release_stale_sync_lock_if_needed(timeout_seconds=10)
        return {
            "status": company.woo_last_sync_status or "pending",
            "phase": company.odoo_sync_progress_phase or "",
            "error_message": company.woo_last_error_message or "",
        }