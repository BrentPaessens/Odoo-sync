/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { onWillUnmount } from "@odoo/owl";
import { FormController } from "@web/views/form/form_controller";

const POLL_INTERVAL_MS = 2000;
const STUCK_TIMEOUT_MS = 10000;

patch(FormController.prototype, {
    setup() {
        super.setup(...arguments);
        this._platformSyncPoller = setInterval(() => {
            this._pollCompanySyncProgress();
        }, POLL_INTERVAL_MS);

        onWillUnmount(() => {
            if (this._platformSyncPoller) {
                clearInterval(this._platformSyncPoller);
                this._platformSyncPoller = null;
            }
        });
    },

    async _pollCompanySyncProgress() {
        const root = this.model && this.model.root;
        if (!root || root.resModel !== "res.company" || !root.resId) {
            return;
        }

        const status = root.data && root.data.woo_last_sync_status;
        if (status !== "syncing") {
            return;
        }

        try {
            if (typeof root.load === "function") {
                await root.load();
                await this._handleStuckSyncWatchdog(root);
                this.render(true);
                return;
            }
        } catch (_error) {
            // Fallback below.
        }

        const now = Date.now();
        if (!this._platformSyncLastReloadAt || now - this._platformSyncLastReloadAt >= 10000) {
            this._platformSyncLastReloadAt = now;
            window.location.reload();
        }
    },

    async _handleStuckSyncWatchdog(root) {
        const data = root.data || {};
        const status = data.woo_last_sync_status;
        if (status !== "syncing") {
            this._platformSyncStuckKey = null;
            this._platformSyncNoProgressSinceMs = null;
            return;
        }

        const percent = Number(data.odoo_sync_progress_percent || 0);
        const hasNoProgress = percent <= 0;
        if (!hasNoProgress) {
            this._platformSyncNoProgressSinceMs = null;
            return;
        }

        if (!this._platformSyncNoProgressSinceMs) {
            this._platformSyncNoProgressSinceMs = Date.now();
        }

        const elapsed = Date.now() - this._platformSyncNoProgressSinceMs;
        if (elapsed < STUCK_TIMEOUT_MS) {
            return;
        }

        const stuckKey = `${root.resId}:no-progress`;
        if (this._platformSyncStuckKey === stuckKey) {
            return;
        }
        this._platformSyncStuckKey = stuckKey;

        try {
            await this.orm.call(
                "res.company",
                "action_mark_stale_sync_failed",
                [[root.resId]],
                { timeout_seconds: 10 }
            );
            await root.load();
            this.render(true);
        } catch (_error) {
            // Ignore server-side watchdog errors; still show user feedback.
        }

        this.notification.add(
            "Sync faalde: geen voortgang gedetecteerd binnen 10 seconden. Je kan nu opnieuw starten.",
            {
                type: "danger",
                sticky: true,
                title: "Platform Add-ons",
            }
        );
    },
});
