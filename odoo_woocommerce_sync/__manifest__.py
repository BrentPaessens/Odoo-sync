{
    'name': 'Commerce Platform Add-ons',
    'version': '1.0.0',
    'category': 'Integration',
    'summary': 'Platform add-on framework for commerce connectors (WooCommerce now, Shopify next)',
    'description': """
        Commerce Platform Add-ons
        
        Features:
        - Connector-ready add-on settings at company level
        - Automatic order import from WooCommerce
        - Customer verification & B2C/B2B classification
        - Auto-confirm paid orders as sales orders
        - Multi-branch delivery address tracking
        - Configurable per-company sync settings
        - Shared or separate order/product sync intervals
        - Product synchronization (Odoo → WooCommerce)
        
        Configuration:
        - Settings → Companies → Platform Add-ons tab
        - Enable/disable sync
        - Choose shared or separate sync intervals
        - Set order/product sync intervals
        - Configure connector API credentials
        - Set auto-confirmation rules
    """,
    'author': 'Vervio',
    'license': 'LGPL-3',
    'installable': True,
    'application': False,
    'depends': ['base', 'web', 'product', 'sale'],
    'post_init_hook': 'post_init_hook',
    'data': [
        'views/res_company_views.xml',
        'views/sale_order_views.xml',
        'views/product_template_views.xml',
        'security/ir.model.access.csv',
    ],
    'assets': {
        'web.assets_backend': [
            'odoo_woocommerce_sync/static/src/js/sync_progress_polling.js',
            'odoo_woocommerce_sync/static/src/js/sale_order_sync_list.js',
            'odoo_woocommerce_sync/static/src/xml/sale_order_sync_list.xml',
            'odoo_woocommerce_sync/static/src/js/product_template_sync_list.js',
            'odoo_woocommerce_sync/static/src/xml/product_template_sync_list.xml',
        ],
    },
    'images': [],
}
