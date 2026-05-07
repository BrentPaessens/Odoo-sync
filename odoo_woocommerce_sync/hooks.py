from odoo import SUPERUSER_ID, api


def post_init_hook(cr, registry):
    """Ensure order/product sync cron records exist for all companies after module install/upgrade."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["res.company"].search([])._sync_scheduler_cron_state()
