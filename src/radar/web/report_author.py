"""Route new reports to the current report kit; retain the explicit legacy switch."""

from radar.web.configuration import configuration


def generate(store, ident, run_id, *, telegram=None):
    if configuration(store.root).get("RADAR_REPORT_KIT", "").lower() in {"1", "true"}:
        from radar.reports.pipeline_v8 import generate as implementation

        return implementation(store, ident, run_id, telegram=telegram)
    else:
        from radar.web.author import generate as implementation
    return implementation(store, ident, run_id)
