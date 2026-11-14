import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

class PricingEngine:
    def __init__(
        self,
        dropshipping_fee: float = 2.50,
        pick_and_pack_fee: float = 1.50,
        shipping_cost: float = 4.95,
        margin_percent: float = 0.25,
        marketplace_commission_rate: float = 0.15
    ):
        self.dropshipping_fee = dropshipping_fee
        self.pick_and_pack_fee = pick_and_pack_fee
        self.shipping_cost = shipping_cost
        self.margin_percent = margin_percent
        self.marketplace_commission_rate = marketplace_commission_rate

    def calculate_price(self, supplier_price: float) -> Dict[str, Any]:
        """
        Calculates the full cost and the final selling price.
        
        Logic:
        Base Cost = Supplier Price + Dropshipping Fee + Pick/Pack + Shipping
        Required Revenue (to cover margin and commission) = Base Cost / (1 - margin - commission)
        """
        base_cost = supplier_price + self.dropshipping_fee + self.pick_and_pack_fee + self.shipping_cost
        
        # To ensure the margin is actually achieved after commission is taken from the FINAL price:
        # Final Price * (1 - commission) * (1 - margin) = Base Cost
        # Final Price = Base Cost / ((1 - commission) * (1 - margin))
        
        denominator = (1 - self.marketplace_commission_rate) * (1 - self.margin_percent)
        
        if denominator <= 0:
            raise ValueError("Invalid margin or commission settings.")

        final_price = base_cost / denominator
        
        return {
            "supplier_price": round(supplier_price, 2),
            "base_cost": round(base_cost, 2),
            "final_price": round(final_price, 2),
            "break_even": round(base_cost / (1 - self.marketplace_commission_rate), 2)
        }
