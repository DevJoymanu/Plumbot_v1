"""
The Plumbot flow map, served inside the dashboard.

WHAT: `/platform/flow-map/` returns the interactive map (the lead's journey,
the reply ladder and the full step-by-step map) built by
`docs/flow/build_flow_map.py` from `docs/flow/flow_spec.py` and the code.

WHY IT IS SERVED RATHER THAN COMMITTED: the page carries line numbers and code
excerpts, so a committed copy changed on almost every commit (146 of 153 in a
month touched bot/), which made every diff noisy and every parallel commit a
merge conflict. Built from the DEPLOYED code instead, it is always the map of
what is actually running, and nobody has to remember to republish it.

WHY SUPERUSERS ONLY: it shows source code, prompts and every tenant-neutral
rule of the product. Tenant staff never see platform internals (the same gate
as the Platform console).

HOW: start.sh builds `docs/flow/plumbot-flow.html` when the web service boots
(next to collectstatic). This view serves that file; if it is missing (a local
runserver, or the deploy-time build failed) it builds the page in-process once
and keeps it for the life of the process. A build that fails on a broken
pointer answers 503 with the reason, never a stale or half-built page. Pinned
by FlowMapViewTests in bot/test_views_actions.py.
"""
import importlib.util
import os
import threading

from django.conf import settings
from django.http import HttpResponse

from ..decorators import superuser_required

_DOCS_FLOW = os.path.join(settings.BASE_DIR, 'docs', 'flow')
_BUILT = os.path.join(_DOCS_FLOW, 'plumbot-flow.html')
_cache = {'html': None}
_lock = threading.Lock()


def _build_in_process():
    """Build the page from the code this process is running. Raises ValueError
    (with the broken pointers) when the spec no longer matches the code."""
    spec = importlib.util.spec_from_file_location(
        'plumbot_flow_builder', os.path.join(_DOCS_FLOW, 'build_flow_map.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build_html()


def _page():
    with _lock:
        if _cache['html'] is None:
            if os.path.exists(_BUILT):
                with open(_BUILT, encoding='utf-8') as fh:
                    _cache['html'] = fh.read()
            else:
                _cache['html'] = _build_in_process()
        return _cache['html']


@superuser_required
def flow_map(request):
    try:
        html = _page()
    except ValueError as exc:
        return HttpResponse(
            f"The flow map could not be built from this code: {exc}. "
            "Fix docs/flow/flow_spec.py.", status=503, content_type='text/plain')
    # The page's own <meta charset> sits inside it; the skeleton tags are
    # added by the browser, exactly as when the file is opened locally.
    return HttpResponse('<!doctype html>\n' + html, content_type='text/html; charset=utf-8')
