"""
Public click-to-call bridge for the portfolio PDF.

The portfolio PDF's "Call" button can't use a raw `tel:` link: the in-app PDF
previewers most leads use (Gmail / Google Drive / WhatsApp on Android) refuse the
`tel:` scheme and spin forever trying to load it as a web page. Instead the button
points at this https page, which every previewer opens in a real browser — and the
browser *does* hand `tel:` to the dialer. Net effect: tapping Call opens the phone
app with the number pre-filled, in every viewer.

No auth: this is a customer-facing endpoint reached from the PDF.
"""

from django.http import HttpResponse
from django.views.decorators.http import require_GET

# Business call line — kept in sync with the PDF's Call button and
# customer_emails._PLUMBER_PHONE.
CALL_NUMBER_E164    = "+263774819901"
CALL_NUMBER_DISPLAY = "+263 77 481 9901"

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Call HomeBase Plumbers</title>
<meta http-equiv="refresh" content="0; url=tel:{e164}">
<style>
  html,body{{margin:0;height:100%;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
    background:#0f1115;color:#fff;display:flex;align-items:center;justify-content:center}}
  .card{{text-align:center;padding:32px 24px;max-width:360px}}
  h1{{font-size:20px;margin:0 0 6px}}
  p{{color:#9aa0a6;font-size:14px;margin:0 0 22px}}
  .btn{{display:inline-block;background:#25D366;color:#fff;text-decoration:none;
    font-size:17px;font-weight:600;padding:14px 26px;border-radius:10px}}
  .num{{display:block;margin-top:16px;color:#cfd3d8;font-size:15px}}
</style>
</head>
<body>
  <div class="card">
    <h1>HomeBase Plumbers</h1>
    <p>Opening your phone dialer…</p>
    <a class="btn" href="tel:{e164}">Call {display}</a>
    <span class="num">{display}</span>
  </div>
  <script>
    // Fire the dialer right away; the button above is the manual fallback.
    try {{ window.location.href = "tel:{e164}"; }} catch (e) {{}}
  </script>
</body>
</html>
"""


@require_GET
def call_redirect(request):
    """Land any viewer on an https page, then hand off to the phone dialer."""
    html = _PAGE.format(e164=CALL_NUMBER_E164, display=CALL_NUMBER_DISPLAY)
    resp = HttpResponse(html)
    # Don't let a previewer/proxy cache a stale number.
    resp["Cache-Control"] = "no-store"
    return resp
