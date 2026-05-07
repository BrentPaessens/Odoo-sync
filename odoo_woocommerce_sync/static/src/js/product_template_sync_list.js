/** @odoo-module **/

import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";
import { ListController } from "@web/views/list/list_controller";
import { listView } from "@web/views/list/list_view";
import { KanbanController } from "@web/views/kanban/kanban_controller";
import { kanbanView } from "@web/views/kanban/kanban_view";

async function runProductSyncWithNotifications(controller) {
    let closeInProgressNotification;
    try {
        closeInProgressNotification = controller.env.services.notification.add(
            _t("Bezig met synchroniseren van producten. Even geduld..."),
            {
                title: _t("Producten synchroniseren"),
                type: "info",
                sticky: true,
            }
        );

        const result = await controller.env.services.orm.call(
            "product.template",
            "action_sync_platform_products",
            [[]]
        );

        if (result?.company_id) {
            await pollProductSyncStatus(controller, result.company_id, closeInProgressNotification);
            return;
        }

        closeInProgressNotification?.();
    } catch (error) {
        closeInProgressNotification?.();
        throw error;
    }
}

async function pollProductSyncStatus(controller, companyId, closeInProgressNotification) {
    const pollIntervalMs = 3000;
    const maxPollAttempts = 200;

    for (let attempt = 0; attempt < maxPollAttempts; attempt++) {
        await new Promise((resolve) => setTimeout(resolve, pollIntervalMs));

        const syncStatus = await controller.env.services.orm.call(
            "product.template",
            "action_get_platform_product_sync_status",
            [[], companyId]
        );

        if (syncStatus?.status === "syncing") {
            continue;
        }

        closeInProgressNotification?.();

        if (syncStatus?.status === "success") {
            controller.env.services.notification.add(
                _t("Producten synchroniseren is gereed. Ververs de pagina om de nieuwste producten te zien."),
                {
                    title: _t("Producten synchroniseren gereed"),
                    type: "success",
                    sticky: true,
                }
            );
            return;
        }

        if (syncStatus?.status === "failed") {
            controller.env.services.notification.add(
                syncStatus?.error_message || _t("Producten synchroniseren is mislukt."),
                {
                    title: _t("Producten ophalen mislukt"),
                    type: "danger",
                    sticky: true,
                }
            );
            return;
        }

        controller.env.services.notification.add(
            _t("Producten ophalen is gestopt. Ververs de pagina om de actuele status te zien."),
            {
                title: _t("Producten ophalen"),
                type: "info",
                sticky: true,
            }
        );
        return;
    }

    closeInProgressNotification?.();
    controller.env.services.notification.add(
        _t("Producten ophalen duurt langer dan verwacht. Ververs later de pagina om de status te controleren."),
        {
            title: _t("Producten ophalen"),
            type: "info",
            sticky: false,
        }
    );
}

class WooProductTemplateSyncListController extends ListController {
    async onClickSyncProducts() {
        await runProductSyncWithNotifications(this);
    }

    get syncProductsButtonLabel() {
        return _t("Producten Synchroniseren");
    }
}

WooProductTemplateSyncListController.template = "odoo_woocommerce_sync.WooProductTemplateSyncListView";

const wooProductTemplateSyncListView = {
    ...listView,
    Controller: WooProductTemplateSyncListController,
};

registry.category("views").add("woo_product_template_sync_list", wooProductTemplateSyncListView);

class WooProductTemplateSyncKanbanController extends KanbanController {
    async onClickSyncProducts() {
        await runProductSyncWithNotifications(this);
    }

    get syncProductsButtonLabel() {
        return _t("Producten Synchroniseren");
    }
}

WooProductTemplateSyncKanbanController.template = "odoo_woocommerce_sync.WooProductTemplateSyncKanbanView";

const wooProductTemplateSyncKanbanView = {
    ...kanbanView,
    Controller: WooProductTemplateSyncKanbanController,
};

registry.category("views").add("woo_product_template_sync_kanban", wooProductTemplateSyncKanbanView);