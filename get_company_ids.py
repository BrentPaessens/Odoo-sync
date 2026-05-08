"""
get_company_ids.py
──────────────────
Fetch all company IDs and names from Odoo.

Usage:
  python get_company_ids.py
"""

import sys
from pathlib import Path

# Load .env manually from Shopify folder
def load_env_file(path):
    """Load key=value pairs from .env file."""
    env_vars = {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    env_vars[key.upper()] = value
    except FileNotFoundError:
        return None
    return env_vars

# Load from Shopify/.env
env_path = Path(__file__).parent / "Shopify" / ".env"
env_vars = load_env_file(env_path)

if not env_vars:
    print("Error: Could not find Shopify/.env")
    sys.exit(1)

odoo_url = env_vars.get('ODOO_URL', '').rstrip('/')
odoo_db = env_vars.get('ODOO_DB', '')
odoo_username = env_vars.get('ODOO_USERNAME', '')
odoo_password = env_vars.get('ODOO_PASSWORD', '')
odoo_api_key = env_vars.get('ODOO_API_KEY', '')

import httpx
import re

def get_companies():
    """Fetch all companies using REST API (JSON-2)."""
    if not odoo_api_key:
        print("Error: ODOO_API_KEY not found in .env")
        return []
    
    client = httpx.Client(
        timeout=30,
        headers={"Authorization": f"Bearer {odoo_api_key}"}
    )
    
    try:
        url = f"{odoo_url}/json/2/res.company/search_read"
        response = client.post(url, json={
            "domain": [],
            "fields": ["id", "name"],
            "order": "id asc",
        })
        response.raise_for_status()
        data = response.json()
        records = data.get("records", data) if isinstance(data, dict) else data
        return records
    except Exception as e:
        print(f"Error fetching companies: {e}")
        return []
    finally:
        client.close()

def main():
    print("\n" + "="*60)
    print("  Odoo Company IDs")
    print("="*60 + "\n")
    
    companies = get_companies()
    
    if not companies:
        print("No companies found.")
        return
    
    print("Companies:\n")
    for company in companies:
        company_id = company.get("id")
        company_name = company.get("name")
        print(f"  ID: {company_id:<3} | Name: {company_name}")
    
    print("\n" + "="*60)
    print("  Update your .env files:")
    print("="*60)
    print("\nWooCommerce/.env (for Business A - Clothing):")
    print('  ODOO_USERNAME="your_woocommerce_user@vervio.be"')
    print("\nShopify/.env (for Business B - Food):")
    print('  ODOO_USERNAME="your_shopify_user@vervio.be"\n')

if __name__ == "__main__":
    main()
