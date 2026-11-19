import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "telegram_egress", Path(__file__).parents[1] / "scripts/enable_telegram_egress.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def config():
    return {
        "inbounds": [{"tag": "model-http", "listen": "127.0.0.1", "protocol": "http"}],
        "routing": {"rules": [
            {"domain": ["full:openrouter.ai"], "port": "443",
             "inboundTag": ["model-http"], "outboundTag": "egress"},
            {"inboundTag": ["model-http"], "network": "tcp,udp", "outboundTag": "block"},
        ]},
    }


def test_restricted_route_is_idempotent_and_keeps_default_block():
    original = config()
    result = module.extend(original)
    assert len(original["routing"]["rules"]) == 2
    assert module.extend(result) == result
    rules = result["routing"]["rules"]
    assert len(rules) == 3 and rules[-1] == original["routing"]["rules"][-1]
    assert rules[0] == original["routing"]["rules"][0]
    assert rules[1]["network"] == "tcp" and rules[1]["port"] == "443"
    assert rules[1]["ip"] == module.TELEGRAM_NETWORKS


def test_refuses_to_extend_public_proxy():
    value = config()
    value["inbounds"][0]["listen"] = "0.0.0.0"
    with pytest.raises(ValueError, match="private loopback"):
        module.extend(value)
