"""
Deprecated alias — use `test_smoke` instead.

  ./scripts/test_smoke.sh
  bench --site <site> execute erpnext.erpnext_integrations.ecommerce_api.test_smoke.run
"""

from erpnext.erpnext_integrations.ecommerce_api.test_smoke import *  # noqa: F401,F403
from erpnext.erpnext_integrations.ecommerce_api.test_smoke import cleanup, run

__all__ = ["run", "cleanup"]
