"""Configuration for supplemental search through the shared model gateway."""

from radar.web.configuration import raw_model_config


def openrouter_search_model(root, cfg):
    if not (cfg.get("OPENROUTER_API_KEY") or "").strip():
        return ""
    role = raw_model_config(root).get("perplexity_search")
    model = role.get("model") if isinstance(role, dict) else None
    return model.strip() if isinstance(model, str) else ""
