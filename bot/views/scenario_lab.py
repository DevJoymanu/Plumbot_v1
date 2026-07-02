"""
Scenario Lab — web UI for the conversation scenario suite.

Lists every TestScenario grouped by category, runs them (all, one category, or
a single scenario) through the EXACT production pipeline with real DeepSeek at
the click of a button — the browser equivalent of `python manage.py
run_scenarios` — and shows per-assertion pass/fail plus full transcripts.
Scenarios are created/edited right on the page, so every new use case (and its
desired response) becomes a permanent automated check.

Runs execute in a background thread (a suite takes ~1 min — too long for a
sync request); the page polls `/scenario-lab/status/` for live progress.
Results are persisted onto each TestScenario row (last_result/last_run_at).
"""
import glob
import json
import os
import threading

from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from ..decorators import staff_required
from ..models import TestScenario
from ..scenario_runner import parse_scenario, run_scenario

# ── Single in-flight run, with live progress the page can poll ────────────────
_run_lock = threading.Lock()
_run_state = {
    "running": False,
    "total": 0,          # scenarios in this run
    "done": 0,           # scenarios finished
    "current": "",       # scenario name in flight
    "results": [],       # per-scenario result dicts (scenario_runner shape)
    "error": "",
    "finished_at": None,
}


def _seed_from_files():
    """Import repo scenarios/*.txt into the DB once (by name) so the Lab starts
    populated. DB is the source of truth afterwards — file edits don't overwrite
    a row that already exists."""
    for path in sorted(glob.glob("scenarios/*.txt")):
        name = os.path.basename(path).replace(".txt", "").replace("_", " ")
        if TestScenario.objects.filter(name=name).exists():
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
            TestScenario.objects.create(
                name=name, category="Seeded", content=content,
                description="Imported from repo scenarios/",
            )
        except OSError:
            continue


def _execute_run(scenarios):
    """Background thread body: run each scenario, persist + publish results."""
    global _run_state
    try:
        for sc in scenarios:
            with _run_lock:
                _run_state["current"] = sc.name
            try:
                result = run_scenario(sc.name, sc.content)
            except ValueError as exc:
                result = {"name": sc.name, "sender": "", "passed": 0,
                          "failed": 1, "turns": [],
                          "error": f"Parse error: {exc}"}
            sc.last_result = result
            sc.last_run_at = timezone.now()
            sc.save(update_fields=["last_result", "last_run_at"])
            with _run_lock:
                _run_state["results"].append(result)
                _run_state["done"] += 1
    except Exception as exc:  # pragma: no cover — surfaced to the UI
        with _run_lock:
            _run_state["error"] = str(exc)
    finally:
        with _run_lock:
            _run_state["running"] = False
            _run_state["current"] = ""
            _run_state["finished_at"] = timezone.now().isoformat()


@staff_required
def scenario_lab_view(request):
    _seed_from_files()
    scenarios = list(TestScenario.objects.all())
    categories = {}
    for sc in scenarios:
        categories.setdefault(sc.category or "General", []).append(sc)
    return render(request, "bot/pages/scenario_lab.html", {
        "active_nav": "scenario_lab",
        "categories": sorted(categories.items()),
        "total": len(scenarios),
    })


@staff_required
@require_http_methods(["POST"])
def scenario_lab_run(request):
    """Start a background run: all active scenarios, one category, or one id."""
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        payload = {}

    qs = TestScenario.objects.filter(is_active=True)
    if payload.get("id"):
        qs = TestScenario.objects.filter(pk=payload["id"])
    elif payload.get("category"):
        qs = qs.filter(category=payload["category"])
    scenarios = list(qs)
    if not scenarios:
        return JsonResponse({"ok": False, "error": "No scenarios to run"}, status=400)

    with _run_lock:
        if _run_state["running"]:
            return JsonResponse({"ok": False, "error": "A run is already in progress"},
                                status=409)
        _run_state.update(running=True, total=len(scenarios), done=0,
                          current="", results=[], error="", finished_at=None)

    threading.Thread(target=_execute_run, args=(scenarios,), daemon=True).start()
    return JsonResponse({"ok": True, "total": len(scenarios)})


@staff_required
@require_http_methods(["GET"])
def scenario_lab_status(request):
    with _run_lock:
        state = {k: v for k, v in _run_state.items()}
    return JsonResponse({"ok": True, **state})


@staff_required
@require_http_methods(["POST"])
def scenario_lab_save(request):
    """Create or update a scenario from the page's editor."""
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    name = (payload.get("name") or "").strip()
    content = (payload.get("content") or "").strip()
    if not name or not content:
        return JsonResponse({"ok": False, "error": "Name and content are required"},
                            status=400)
    # Validate the format up front so a broken scenario can't be saved.
    try:
        turns = parse_scenario(content, origin=name)
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    if not turns:
        return JsonResponse({"ok": False, "error": "Scenario has no customer messages"},
                            status=400)

    defaults = {
        "content": content,
        "category": (payload.get("category") or "General").strip() or "General",
        "description": (payload.get("description") or "").strip(),
        "is_active": bool(payload.get("is_active", True)),
    }
    if payload.get("id"):
        updated = TestScenario.objects.filter(pk=payload["id"]).update(
            name=name, **defaults)
        if not updated:
            return JsonResponse({"ok": False, "error": "Scenario not found"}, status=404)
        sc = TestScenario.objects.get(pk=payload["id"])
    else:
        if TestScenario.objects.filter(name=name).exists():
            return JsonResponse({"ok": False, "error": f"'{name}' already exists"},
                                status=400)
        sc = TestScenario.objects.create(name=name, **defaults)
    return JsonResponse({"ok": True, "id": sc.pk})


@staff_required
@require_http_methods(["POST"])
def scenario_lab_delete(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)
    deleted, _ = TestScenario.objects.filter(pk=payload.get("id")).delete()
    return JsonResponse({"ok": bool(deleted)})


@staff_required
@require_http_methods(["GET"])
def scenario_lab_detail(request, pk):
    """Full scenario (content + last result) for the editor / results panel."""
    sc = TestScenario.objects.filter(pk=pk).first()
    if not sc:
        return JsonResponse({"ok": False, "error": "Not found"}, status=404)
    return JsonResponse({"ok": True, "scenario": {
        "id": sc.pk, "name": sc.name, "category": sc.category,
        "description": sc.description, "content": sc.content,
        "is_active": sc.is_active, "last_status": sc.last_status,
        "last_run_at": sc.last_run_at.isoformat() if sc.last_run_at else None,
        "last_result": sc.last_result,
    }})
