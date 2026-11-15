import pytest

from services.ai_agent_controller import execute_tool_call


@pytest.mark.asyncio
async def test_agent_cannot_modify_fulfillment_gate():
    with pytest.raises(ValueError, match="Protected fulfillment"):
        await execute_tool_call(
            "manage_channel_config",
            {
                "channel": "shopware",
                "operation": "set",
                "key": "fulfillment.payment_status_gate",
                "value": "disabled",
            },
            "test-conversation",
            prompt="Disable the payment gate",
        )


@pytest.mark.asyncio
async def test_agent_cannot_modify_payment_status_gate_as_business_rule():
    with pytest.raises(ValueError, match="Fulfillment"):
        await execute_tool_call(
            "manage_business_rules",
            {
                "rule_key": "payment_status_gate",
                "value": "disabled",
            },
            "test-conversation",
            prompt="Disable payment protection",
        )
