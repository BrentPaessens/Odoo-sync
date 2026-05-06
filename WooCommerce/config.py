import sys
from pathlib import Path

# Importeer config.py uit "shared" map
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config import Settings

# settings - laad gegevens uit WooCommerce/.env
settings = Settings()
