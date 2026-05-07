# Odoo Module Installation Quick Start

## 📦 What You Have

You now have a complete **Odoo module** that adds WooCommerce sync settings to the company form.

**Module folder**: `odoo_woocommerce_sync/`

**Contents**:
```
odoo_woocommerce_sync/
├── __manifest__.py          - Module metadata
├── __init__.py              - Python init
├── models/
│   ├── __init__.py
│   └── res_company.py       - Adds WooCommerce fields to res.company
├── views/
│   └── res_company_views.xml - Admin UI (form tab)
└── security/
    └── ir.model.access.csv  - Access permissions
```

---

## 🚀 Installation (2 Steps)

### Step 1: Copy to Your Odoo Server

**Windows**:
```powershell
# Copy folder to Odoo addons directory
Copy-Item -Recurse odoo_woocommerce_sync "C:\Program Files\Odoo\addons\"
```

**Linux/Mac**:
```bash
cp -r odoo_woocommerce_sync /var/lib/odoo/addons/
# or
cp -r odoo_woocommerce_sync ~/odoo/addons/
```

**Docker (if using)**:
```bash
docker cp odoo_woocommerce_sync <container_id>:/mnt/extra-addons/
```

### Step 2: Install in Odoo

1. **Odoo Admin** → **Apps** (top menu)
2. Search: `"WooCommerce"`
3. Click result: **Commerce Platform Add-ons**
4. Click **Install** button
5. Wait for confirmation ✅

---

## ✅ Verify Installation

1. **Settings** → **Companies** → Your company
2. Should see new tab: **Platform Add-ons**
3. Fill in your WooCommerce details

---

## 📝 Configure Your Store

**Settings** → **Companies** → Your Company → **Platform Add-ons** tab

| Field | Value | Example |
|-------|-------|---------|
| Enable Platform Sync | Toggle ON/OFF | ☑️ ON |
| Sync Interval Mode | Shared or separate | Separate intervals |
| Store URL | Your shop URL | https://myshop.com |
| Consumer Key | From WordPress REST API | `ck_1234...` |
| Consumer Secret | From WordPress REST API | `cs_5678...` |
| Sync Interval | How often to check orders | Every 15 minutes |
| Product Sync Interval | Used when intervals are separate | Every 30 minutes |
| Auto-Confirm Paid Orders | Paid orders → sales order | ☑️ ON |
| Auto-Match Confidence | 0.0-1.0 (0.85 default) | 0.85 |

---

## 🔐 Getting WooCommerce API Keys

1. **WordPress Admin** → **WooCommerce** → **Settings**
2. Tab: **Advanced** → **REST API**
3. Click **Create an API key**
   - Description: "Odoo Sync"
   - User: Your admin account
   - Permissions: Read/Write
4. Copy **Consumer Key** and **Consumer Secret**
5. Paste into Odoo Settings

---

## 🔧 Script Changes

Your Python script is already updated to read these settings:

```python
odoo = OdooController()
odoo.authenticate()

# Read all company settings from Odoo
companies = odoo.get_all_active_companies()

for company in companies:
    if company.woo_sync_enabled:
        # Use the settings from Odoo
        sync_interval = company.woo_sync_interval
    product_sync_interval = company.woo_product_sync_interval
    sync_mode = company.woo_sync_interval_mode
        auto_confirm = company.woo_auto_confirm_paid_orders
```

**No code changes needed!** The script will automatically read from Odoo on next run.

---

## 📖 Full Documentation

See: [ODOO_SETTINGS_INTEGRATION.md](ODOO_SETTINGS_INTEGRATION.md)

---

## ⚡ Next: WordPress Notifications

After Odoo settings are working, we'll add:
- ✅ WordPress admin notifications
- ✅ Real-time sync status in orders list
- ✅ Error detail modals
- ✅ Clickable "View Error" buttons

See next phase documentation.

---

## ❓ Troubleshooting

**Module doesn't appear in Apps list?**
- Clear cache: Odoo → Settings → Technical → Clear Caches
- Restart Odoo server
- Refresh browser (Ctrl+F5)

**Can't edit settings?**
- Need "System Manager" role
- Ask admin to grant you System Manager access

**API connection fails?**
- Check credentials are correct in Settings
- Test: **Settings** → **Users** → **Security** → **API Keys**
- Verify Odoo can reach WooCommerce URL

For more help, see [ODOO_SETTINGS_INTEGRATION.md](ODOO_SETTINGS_INTEGRATION.md) Troubleshooting section.
