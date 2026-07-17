"""
Rendered pricing copy (Phase 2.3c — docs/MULTI_TENANT_PLAN.md §3.3).

build_structured_pricing(cfg) renders the per-intent bilingual price blocks
(the dict that lived hardcoded in response_mixin) from the tenant's
TenantPriceItem rows. The SENTENCES are platform sales copy; the FIGURES are
the tenant's business facts. An intent whose figures aren't on the tenant's
sheet is omitted entirely — the service-inquiry handler then deflects to the
free site visit instead of quoting money that doesn't exist (and never
another tenant's).

Byte-identity: rendering with homebase's seed reproduces the legacy block
character-for-character (pinned by tests/pinned_structured_pricing.py).
"""

from __future__ import annotations


def _figures(cfg):
    """Flatten the tenant's price rows into the named figures the templates
    use. Returns None for any missing value."""
    def g(family, variant=''):
        return cfg.price_item(family, variant)

    def num(item, field):
        if item is None:
            return None
        value = getattr(item, field, None)
        if value is None:
            return None
        return int(value) if value == int(value) else float(value)

    fs = g('tub', 'freestanding')
    fs_parts = {p.get('name'): p.get('amount') for p in (fs.parts if fs else []) or []}

    return dict(
        fs_supply=fs_parts.get('tub'), fs_mixer=fs_parts.get('mixer'),
        fs_install=fs_parts.get('install'), fs_allin=num(fs, 'allin'),
        tub_s=num(g('tub'), 'supply'), tub_l=num(g('tub'), 'labour'), tub_allin=num(g('tub'), 'allin'),
        ch_s=num(g('chamber'), 'supply'), ch_l=num(g('chamber'), 'labour'), ch_allin=num(g('chamber'), 'allin'),
        gey_s=num(g('geyser'), 'supply'), gey_l=num(g('geyser'), 'labour'), gey_allin=num(g('geyser'), 'allin'),
        sh_s=num(g('shower'), 'supply'), sh_l=num(g('shower'), 'labour'), sh_allin=num(g('shower'), 'allin'),
        va_s=num(g('vanity'), 'supply'), va_l=num(g('vanity'), 'labour'), va_allin=num(g('vanity'), 'allin'),
        to_s=num(g('toilet'), 'supply'), to_l=num(g('toilet'), 'labour'), to_allin=num(g('toilet'), 'allin'),
        wh_s=num(g('toilet', 'wall_hung'), 'supply'), wh_l=num(g('toilet', 'wall_hung'), 'labour'),
        wh_allin=num(g('toilet', 'wall_hung'), 'allin'),
        fb=num(g('package', 'facebook'), 'flat'),
        drain_simple=num(g('repair', 'drain_simple'), 'labour'),
        drain_severe=num(g('repair', 'drain_severe'), 'labour'),
        jetting=num(g('repair', 'jetting'), 'flat'),
        tap=num(g('repair', 'leaking_tap'), 'labour'),
        minor_leak=num(g('repair', 'minor_pipe_leak'), 'labour'),
        burst=num(g('repair', 'burst_pipe'), 'labour'),
        pipe_section=num(g('repair', 'pipe_section'), 'labour'),
        thermo=num(g('geyser_service', 'thermostat'), 'labour'),
        element=num(g('geyser_service', 'element'), 'labour'),
        valve=num(g('geyser_service', 'pressure_valve'), 'labour'),
        gey_repl=num(g('geyser_service', 'replacement'), 'allin'),
        cistern=num(g('repair', 'cistern'), 'labour'),
        seat_s=num(g('repair', 'toilet_seat_replacement'), 'supply'),
        seat_f=num(g('repair', 'toilet_seat_replacement'), 'labour'),
        base=num(g('repair', 'toilet_base'), 'labour'),
        trepl_s=num(g('repair', 'full_toilet_replacement'), 'supply'),
        trepl_i=num(g('repair', 'full_toilet_replacement'), 'labour'),
    )


def facebook_package_facts(cfg):
    """The tenant's social-ad package for copy composition, or None.
    {'price': int, 'label': 'Facebook package', 'names': [...],
     'en': 'freestanding tub and side chamber', 'sn': '… ne …'}"""
    item = cfg.price_item('package', 'facebook')
    if item is None or item.flat is None:
        return None
    names = [p.get('name') for p in (item.parts or []) if p.get('name')]
    label = (item.label or 'Facebook Package')
    return {
        'price': int(item.flat),
        'label': label[:1].upper() + label[1:].lower(),  # "Facebook package"
        'names': names,
        'en': ' and '.join(names),
        'sn': ' ne '.join(names),
    }


def build_prompt_pricing_guide(cfg) -> str:
    """The PRICING GUIDE block injected into the DeepSeek system prompt —
    one '- ' line per figure the tenant actually has. With no price sheet the
    guide becomes an explicit do-not-state-prices instruction, so the LLM
    never invents money."""
    f = _figures(cfg)
    lines = []
    if f['to_s'] is not None and f['to_l'] is not None:
        lines.append(f"- Toilet: supply from US${f['to_s']}, install from US${f['to_l']}")
    if f['sh_s'] is not None and f['sh_l'] is not None:
        lines.append(f"- Shower cubicle (900x900mm): supply from US${f['sh_s']}, install from US${f['sh_l']}")
    if f['va_s'] is not None and f['va_l'] is not None:
        lines.append(f"- Vanity unit: supply from US${f['va_s']}, install from US${f['va_l']}")
    if f['gey_s'] is not None and f['gey_l'] is not None:
        lines.append(f"- Geyser: supply from US${f['gey_s']}, install from US${f['gey_l']}")
    if f['tub_s'] is not None and f['tub_l'] is not None and f['tub_allin'] is not None:
        lines.append(
            f"- Bathtub (ordinary/built-in, INCLUDING corner tubs): supply from US${f['tub_s']}, "
            f"install from US${f['tub_l']} → from US${f['tub_allin']} all-in")
    if all(f[k] is not None for k in ('fs_supply', 'fs_mixer', 'fs_install', 'fs_allin')):
        lines.append(
            f"- Freestanding tub: supply from US${f['fs_supply']}, mixer from US${f['fs_mixer']}, "
            f"install US${f['fs_install']} → from US${f['fs_allin']} all-in")
    if f['tub_allin'] is not None:
        lines.append(
            f"- A CORNER tub is a built-in tub → from US${f['tub_allin']} all-in, NOT the freestanding price.")
    if f['ch_s'] is not None and f['ch_l'] is not None and f['ch_allin'] is not None:
        lines.append(
            f"- Side chamber: supply from US${f['ch_s']}, install from US${f['ch_l']} → from US${f['ch_allin']} all-in")
    full_pkg = cfg.price_item('package', 'full_bathroom')
    if full_pkg is not None and full_pkg.flat is not None:
        lines.append(f"- Full bathroom package: from US${int(full_pkg.flat)}+")
    lines.append("- Site assessment / visit: FREE")
    if len(lines) == 1:
        return (
            "- This business has not published price figures. Do NOT state or "
            "estimate any prices — the free site visit confirms the exact figure.\n"
            "        - Site assessment / visit: FREE"
        )
    return "\n        ".join(lines)


def build_structured_pricing(cfg) -> dict:
    """The per-intent bilingual pricing blocks for a tenant. Intents whose
    figures are missing from the tenant's sheet are omitted."""
    f = _figures(cfg)
    out = {}

    def have(*keys):
        return all(f[k] is not None for k in keys)

    tub_breakdown = None
    if have('fs_supply', 'fs_mixer', 'fs_install', 'fs_allin', 'tub_s', 'tub_l',
            'tub_allin', 'ch_s', 'ch_l', 'ch_allin'):
        tub_breakdown = [
            f"Freestanding tub: Supply US${f['fs_supply']} | Mixer US${f['fs_mixer']} | Install US${f['fs_install']} → from US${f['fs_allin']} all-in",
            f"Standard built-in tub: Supply from US${f['tub_s']} | Install from US${f['tub_l']} → from US${f['tub_allin']} all-in",
            f"Side chamber (add-on): Supply from US${f['ch_s']} | Install from US${f['ch_l']} → from US${f['ch_allin']}",
        ]
        tub_sn_breakdown = [
            f"Freestanding tub: Supply US${f['fs_supply']} | Mixer US${f['fs_mixer']} | Install US${f['fs_install']} → kubva US${f['fs_allin']} all-in",
            f"Standard tub: Supply kubva US${f['tub_s']} | Install kubva US${f['tub_l']} → kubva US${f['tub_allin']} all-in",
            f"Side chamber (add-on): Supply kubva US${f['ch_s']} | Install kubva US${f['ch_l']} → kubva US${f['ch_allin']}",
        ]

    if tub_breakdown is not None:
        out["tub_sales"] = {
            "breakdown_lines": list(tub_breakdown),
            "total_line": f"Full freestanding setup from US${f['fs_allin']} all-in (tub US${f['fs_supply']} + mixer US${f['fs_mixer']} + install US${f['fs_install']}). Standard built-in tubs from US${f['tub_allin']} all-in.",
            "cheapest_line": f"Side chamber adds US${f['ch_s']} supply + US${f['ch_l']} install.",
            "sn_breakdown_lines": list(tub_sn_breakdown),
            "sn_total_line": f"Full freestanding setup kubva US${f['fs_allin']}. Standard tub kubva US${f['tub_allin']} all-in.",
            "sn_cheapest_line": f"Starting point i standard tub paUS${f['tub_s']} supply + US${f['tub_l']} install.",
        }
        out["standalone_tub"] = {
            "breakdown_lines": list(tub_breakdown),
            "total_line": f"Full freestanding setup from US${f['fs_allin']} all-in.",
            "cheapest_line": f"If budget is tight, the standard built-in tub starts from US${f['tub_allin']} all-in.",
            "sn_breakdown_lines": list(tub_sn_breakdown),
            "sn_total_line": f"Full freestanding setup kubva US${f['fs_allin']} all-in.",
            "sn_cheapest_line": f"Budget option i standard built-in tub kubva US${f['tub_allin']} all-in.",
        }
        out["bathtub_installation"] = {
            "breakdown_lines": list(tub_breakdown),
            "total_line": f"Full freestanding setup from US${f['fs_allin']}. Standard tub from US${f['tub_allin']} all-in.",
            "cheapest_line": f"Standard built-in tub is the entry point at US${f['tub_allin']} all-in.",
            "sn_breakdown_lines": list(tub_sn_breakdown),
            "sn_total_line": f"Full freestanding setup kubva US${f['fs_allin']}. Standard tub kubva US${f['tub_allin']} all-in.",
            "sn_cheapest_line": f"Standard built-in tub i entry point paUS${f['tub_allin']} all-in.",
        }

    if have('gey_s', 'gey_l', 'gey_allin'):
        out["geyser"] = {
            "breakdown_lines": [f"Geyser: Supply from US${f['gey_s']}, Install from US${f['gey_l']}"],
            "total_line": f"Geysers start from US${f['gey_allin']} all-in — supply and install.",
            "cheapest_line": f"Already have the geyser? Install-only from US${f['gey_l']}.",
            "sn_breakdown_lines": [f"Geyser: Supply kubva US${f['gey_s']}, Install kubva US${f['gey_l']}"],
            "sn_total_line": f"Geysers dzinotangira paUS${f['gey_allin']} all-in — supply ne install.",
            "sn_cheapest_line": f"Muchitova ne geyser? Install chete kubva US${f['gey_l']}.",
        }

    if have('sh_s', 'sh_l', 'sh_allin'):
        out["shower_cubicle"] = {
            "breakdown_lines": [f"Shower cubicle: Supply from US${f['sh_s']}, Install from US${f['sh_l']}"],
            "total_line": f"Shower cubicles start from US${f['sh_allin']} all-in — supply and install.",
            "cheapest_line": f"Already have the cubicle? Install-only from US${f['sh_l']}.",
            "sn_breakdown_lines": [f"Shower cubicle: Supply kubva US${f['sh_s']}, Install kubva US${f['sh_l']}"],
            "sn_total_line": f"Shower cubicles dzinotangira paUS${f['sh_allin']} all-in — supply ne install.",
            "sn_cheapest_line": f"Muchitova ne cubicle? Install chete kubva US${f['sh_l']}.",
        }

    if have('va_s', 'va_l', 'va_allin'):
        out["vanity"] = {
            "breakdown_lines": [f"Vanity unit: Supply from US${f['va_s']}, Install from US${f['va_l']}"],
            "total_line": f"Vanities start from US${f['va_allin']} all-in — supply and install.",
            "cheapest_line": f"Already have the unit? Install-only from US${f['va_l']}.",
            "sn_breakdown_lines": [f"Vanity unit: Supply kubva US${f['va_s']}, Install kubva US${f['va_l']}"],
            "sn_total_line": f"Vanities dzinotangira paUS${f['va_allin']} all-in — supply ne install.",
            "sn_cheapest_line": f"Muchitova ne vanity? Install chete kubva US${f['va_l']}.",
        }

    if have('to_s', 'to_l', 'to_allin'):
        out["toilet"] = {
            "breakdown_lines": [f"Toilet seat: Supply from US${f['to_s']}, Install from US${f['to_l']}"],
            "total_line": f"Toilet replacement starts from US${f['to_allin']} all-in — supply and install.",
            "cheapest_line": f"Already have the toilet? Install-only from US${f['to_l']}.",
            "sn_breakdown_lines": [f"Toilet seat: Supply kubva US${f['to_s']}, Install kubva US${f['to_l']}"],
            "sn_total_line": f"Zvingangoita US${f['to_allin']} yezvinhu zvese pa standard toilet replacement.",
            "sn_cheapest_line": f"Cheapest option installation chete kana muchitova ne toilet — labour inotangira paUS${f['to_l']}.",
        }

    if have('wh_s', 'wh_l', 'wh_allin'):
        # Wall-hung toilet = the chamber install (owner rule): same figures,
        # worded for the toilet ask.
        out["wall_hung_toilet"] = {
            "breakdown_lines": [f"Wall-hung toilet (concealed chamber system): Supply from US${f['wh_s']}, Install from US${f['wh_l']}"],
            "total_line": f"Wall-hung toilet installs start from US${f['wh_allin']} all-in — supply and install.",
            "cheapest_line": f"Already have the unit? Install-only from US${f['wh_l']}.",
            "sn_breakdown_lines": [f"Wall-hung toilet (chamber system): Supply kubva US${f['wh_s']}, Install kubva US${f['wh_l']}"],
            "sn_total_line": f"Zvingangoita US${f['wh_allin']} yezvinhu zvese pa wall-hung toilet system.",
            "sn_cheapest_line": f"Muchitova ne unit? Install chete kubva US${f['wh_l']}.",
        }

    if have('ch_s', 'ch_l', 'ch_allin'):
        out["chamber"] = {
            "breakdown_lines": [f"Side chamber: Supply from US${f['ch_s']}, Install from US${f['ch_l']}"],
            "total_line": f"Side chambers start from US${f['ch_allin']} all-in — supply and install.",
            "cheapest_line": f"Already have the chamber? Install-only from US${f['ch_l']}.",
            "sn_breakdown_lines": [f"Side chamber: Supply kubva US${f['ch_s']}, Install kubva US${f['ch_l']}"],
            "sn_total_line": f"Zvingangoita US${f['ch_allin']} yezvinhu zvese pa standard chamber setup.",
            "sn_cheapest_line": f"Cheapest option installation chete kana muchitova ne chamber — labour inotangira paUS${f['ch_l']}.",
        }

    _fbp = facebook_package_facts(cfg)
    if _fbp is not None and have('sh_s', 'sh_l', 'va_s', 'va_l', 'to_s', 'to_l',
                                 'ch_s', 'ch_l', 'tub_s', 'tub_l',
                                 'fs_supply', 'fs_mixer', 'fs_install'):
        out["facebook_package"] = {
            "breakdown_lines": [
                f"Shower cubicle: Supply from US${f['sh_s']}, Install from US${f['sh_l']}",
                f"Vanity unit: Supply from US${f['va_s']}, Install from US${f['va_l']}",
                f"Toilet seat: Supply from US${f['to_s']}, Install from US${f['to_l']}",
                f"Side chamber: Supply from US${f['ch_s']}, Install from US${f['ch_l']}",
                f"Tub: Supply from US${f['tub_s']}, Install from US${f['tub_l']}",
                f"Freestanding tub: supply from US${f['fs_supply']}, mixer from US${f['fs_mixer']}, install US${f['fs_install']}",
            ],
            "total_line": (
                f"The {_fbp['label']} is US${f['fb']}"
                + (f" — {_fbp['en']}." if _fbp['en'] else ".")),
            "cheapest_line": "We'll give you the exact price once we've seen the space.",
            "sn_breakdown_lines": [
                f"Shower cubicle: Supply kubva US${f['sh_s']}, Install kubva US${f['sh_l']}",
                f"Vanity unit: Supply kubva US${f['va_s']}, Install kubva US${f['va_l']}",
                f"Toilet seat: Supply kubva US${f['to_s']}, Install kubva US${f['to_l']}",
                f"Side chamber: Supply kubva US${f['ch_s']}, Install kubva US${f['ch_l']}",
                f"Tub: Supply kubva US${f['tub_s']}, Install kubva US${f['tub_l']}",
                f"Free-standing tub mixer: Supply kubva US${f['fs_mixer']}, Install kubva US${f['fs_install']}",
            ],
            "sn_total_line": (
                f"{_fbp['label'][:1].upper() + _fbp['label'][1:]} inosvika US${f['fb']}"
                + (f" — {_fbp['sn']}." if _fbp['sn'] else ".")),
            "sn_cheapest_line": f"Cheapest option i basic package inotangira paUS${f['fb']} zvinhu zvekuwedzera zvisati zvaiswa.",
        }

    if have('drain_simple', 'drain_severe', 'jetting'):
        out["drain_unblocking"] = {
            "breakdown_lines": [
                f"Simple blockage (sink, basin, shower): Labour from US${f['drain_simple']}",
                f"Severe blockage (main drain, sewer line): Labour from US${f['drain_severe']}",
                f"High-pressure jetting (stubborn blockages): from US${f['jetting']}",
            ],
            "total_line": f"Most drain unblocking jobs start from US${f['drain_simple']} for labour — the exact cost depends on how severe and where the blockage is.",
            "cheapest_line": f"A basic sink or basin unblocking starts from US${f['drain_simple']} labour.",
            "sn_breakdown_lines": [
                f"Simple blockage (sink, basin, shower): Labour kubva US${f['drain_simple']}",
                f"Severe blockage (main drain, sewer line): Labour kubva US${f['drain_severe']}",
                f"High-pressure jetting: kubva US${f['jetting']}",
            ],
            "sn_total_line": f"Zvingangoita US${f['drain_simple']} kubva pa labour — zvichienderana nekubinya uye nzvimbo yekubikira.",
            "sn_cheapest_line": f"Basic sink kana basin unblocking inotangira paUS${f['drain_simple']} labour.",
        }

    if have('tap', 'minor_leak', 'burst', 'pipe_section'):
        out["pipe_repair"] = {
            "breakdown_lines": [
                f"Minor leak repair (joint, fitting): Labour from US${f['minor_leak']}",
                f"Burst pipe repair: Labour from US${f['burst']}",
                f"Pipe section replacement: Labour from US${f['pipe_section']}",
                f"Leaking tap washer/cartridge replacement: from US${f['tap']}",
            ],
            "total_line": f"Pipe repairs start from US${f['tap']}–${f['minor_leak']} for minor leaks — cost depends on the pipe size, location, and how accessible it is.",
            "cheapest_line": f"A leaking tap repair starts from US${f['tap']} labour.",
            "sn_breakdown_lines": [
                f"Minor leak repair (joint, fitting): Labour kubva US${f['minor_leak']}",
                f"Burst pipe repair: Labour kubva US${f['burst']}",
                f"Pipe section replacement: Labour kubva US${f['pipe_section']}",
                f"Leaking tap: kubva US${f['tap']}",
            ],
            "sn_total_line": f"Pipe repairs dzinotangira paUS${f['tap']}–${f['minor_leak']} pa minor leaks — zvichienderana ne pipe size, nzvimbo uye kuti inofashikira here.",
            "sn_cheapest_line": f"Leaking tap repair inotangira paUS${f['tap']} labour.",
        }

    if have('thermo', 'element', 'valve', 'gey_repl'):
        out["geyser_repair"] = {
            "breakdown_lines": [
                f"Thermostat replacement: from US${f['thermo']} labour + parts",
                f"Element replacement: from US${f['element']} labour + parts",
                f"Pressure valve replacement: from US${f['valve']} labour + parts",
                f"Full geyser replacement: from US${f['gey_repl']} (supply + install)",
            ],
            "total_line": f"Geyser repairs start from US${f['valve']}–${f['element']} for labour + parts depending on what needs fixing. If the geyser needs replacing, full supply and install starts from US${f['gey_repl']}.",
            "cheapest_line": f"Minor repairs like a valve or thermostat start from US${f['valve']}–${f['thermo']}.",
            "sn_breakdown_lines": [
                f"Thermostat replacement: kubva US${f['thermo']} labour + zvikamu",
                f"Element replacement: kubva US${f['element']} labour + zvikamu",
                f"Pressure valve replacement: kubva US${f['valve']} labour + zvikamu",
                f"Full geyser replacement: kubva US${f['gey_repl']} (supply + install)",
            ],
            "sn_total_line": f"Geyser repairs dzinotangira paUS${f['valve']}–${f['element']} pa labour + zvikamu zvichienderana nezvinoda kugadzirwa.",
            "sn_cheapest_line": f"Minor repairs dzinotangira paUS${f['valve']}–${f['thermo']}.",
        }

    if have('cistern', 'seat_s', 'seat_f', 'base', 'trepl_s', 'trepl_i'):
        trepl_total = f['trepl_s'] + f['trepl_i']
        out["toilet_repair"] = {
            "breakdown_lines": [
                f"Cistern repair (filling valve, flush valve): from US${f['cistern']} labour + parts",
                f"Toilet seat replacement: Supply from US${f['seat_s']}, fit from US${f['seat_f']}",
                f"Leaking toilet base: Labour from US${f['base']}",
                f"Full toilet replacement: Supply from US${f['trepl_s']}, install from US${f['trepl_i']}",
            ],
            "total_line": f"Toilet repairs start from US${f['cistern']} for labour + parts. A full replacement (supply and fit) starts from US${trepl_total}.",
            "cheapest_line": f"A cistern repair starts from US${f['cistern']} labour + parts.",
            "sn_breakdown_lines": [
                f"Cistern repair: kubva US${f['cistern']} labour + zvikamu",
                f"Toilet seat replacement: Supply kubva US${f['seat_s']}, fit kubva US${f['seat_f']}",
                f"Leaking toilet base: Labour kubva US${f['base']}",
                f"Full toilet replacement: Supply kubva US${f['trepl_s']}, install kubva US${f['trepl_i']}",
            ],
            "sn_total_line": f"Toilet repairs dzinotangira paUS${f['cistern']} pa labour + zvikamu.",
            "sn_cheapest_line": f"Cistern repair inotangira paUS${f['cistern']} labour + zvikamu.",
        }

    return out
