/** @odoo-module **/

import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";
import { ListController } from "@web/views/list/list_controller";
import { listView } from "@web/views/list/list_view";

class WooSaleOrderSyncListController extends ListController {
    async onClickSyncOrders() {
        let closeInProgressNotification;
        try {
            closeInProgressNotification = this.env.services.notification.add(
                _t("Bezig met ophalen van orders. Even geduld..."),
                {
                    title: _t("Orders ophalen"),
                    type: "warning",
                    sticky: true,
                }
            );

            const result = await this.env.services.orm.call(
                "sale.order",
                "action_sync_platform_orders",
                [[]]
            );

            if (result?.company_id) {
                await this._pollSyncStatus(result.company_id, closeInProgressNotification);
                return;
            }

            closeInProgressNotification?.();
        } catch (error) {
            closeInProgressNotification?.();
            throw error;
        }
    }

    async _pollSyncStatus(companyId, closeInProgressNotification) {
        const pollIntervalMs = 3000;
        const maxPollAttempts = 200;

        for (let attempt = 0; attempt < maxPollAttempts; attempt++) {
            await new Promise((resolve) => setTimeout(resolve, pollIntervalMs));

            const syncStatus = await this.env.services.orm.call(
                "sale.order",
                "action_get_platform_order_sync_status",
                [[], companyId]
            );

            if (syncStatus?.status === "syncing") {
                continue;
            }

            closeInProgressNotification?.();

            if (syncStatus?.status === "success") {
                this.env.services.notification.add(
                    _t("Orders ophalen is gereed. Ververs de pagina om de nieuwste orders te zien."),
                    {
                        title: _t("Orders opgehaald"),
                        type: "success",
                        sticky: true,
                    }
                );
                return;
            }

            if (syncStatus?.status === "failed") {
                this.env.services.notification.add(
                    syncStatus?.error_message || _t("Orders ophalen is mislukt."),
                    {
                        title: _t("Orders ophalen mislukt"),
                        type: "danger",
                        sticky: true,
                    }
                );
                return;
            }

            this.env.services.notification.add(
                _t("Orders ophalen is gestopt. Ververs de pagina om de actuele status te zien."),
                {
                    title: _t("Orders ophalen"),
                    type: "info",
                    sticky: true,
                }
            );
            return;
        }

        closeInProgressNotification?.();
        this.env.services.notification.add(
            _t("Orders ophalen duurt langer dan verwacht. Ververs later de pagina om de status te controleren."),
            {
                title: _t("Orders ophalen"),
                type: "info",
                sticky: false,
            }
        );
    }

    get syncOrdersButtonLabel() {
        return _t("Orders ophalen");
    }
}

WooSaleOrderSyncListController.template = "odoo_woocommerce_sync.WooSaleOrderSyncListView";

const wooSaleOrderSyncListView = {
    ...listView,
    Controller: WooSaleOrderSyncListController,
};

registry.category("views").add("woo_sale_order_sync_list", wooSaleOrderSyncListView);