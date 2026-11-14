import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

class InventoryGuard:
    def __init__(self, safety_stock_buffer: float = 0.10):
        """
        :param safety_stock_buffer: Percentage to subtract from supplier stock (e.g., 0.10 for 10%)
        """
        self.safety_stock_buffer = safety_stock_buffer

    def apply_safety_buffer(self, supplier_qty: int) -> int:
        """Calculates buffered quantity to prevent overselling."""
        buffered_qty = int(supplier_qty * (1 - self.safety_stock_buffer))
        return max(0, buffered_qty)

    def validate_bundle_stock(self, bundle_sku: str, component_skus: List[str], 
                             product_stocks: Dict[str, int]) -> Dict[str, Any]:
        """
        Enforces the Atomic Bundle Guard rule:
        Bundle Max Stock = Min(Component Stocks)
        
        Returns:
            Dict containing 'valid_stock' (int) and 'is_active' (bool).
            If any component is 0, is_active is False.
        """
        if not component_skus:
            return {"valid_stock": 0, "is_active": False}

        component_stocks = []
        for sku in component_skus:
            stock = product_stocks.get(sku, 0)
            component_stocks.append(stock)
            
            if stock <= 0:
                logger.warning(f"Component {sku} for bundle {bundle_sku} is out of stock. Pausing bundle.")
                return {"valid_stock": 0, "is_active": False}

        # The bundle's stock is limited by its scarcest component
        max_bundle_stock = min(component_stocks)
        
        return {
            "valid_stock": max_bundle_stock,
            "is_active": True
        }
