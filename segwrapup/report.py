"""Self-contained HTML volumetrics report (single-hue magnitude bars, colour chips
as the legend for the viewer label files; light and dark surfaces validated per the
dataviz reference palette)."""
from __future__ import annotations

import html

from .labels import label_color

CSS = """
:root { color-scheme: light dark; }
.viz-root {
  --surface-1: #fcfcfb; --surface-2: #f2f2f0; --border: #dededa;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #767470;
  --series-1: #2a78d6;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .viz-root {
    --surface-1: #1a1a19; --surface-2: #242423; --border: #3a3a38;
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #96958c;
    --series-1: #3987e5;
  }
}
body { margin: 0; background: var(--surface-1); }
.viz-root {
  background: var(--surface-1); color: var(--text-primary);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  padding: 32px; max-width: 900px; margin: 0 auto;
}
h1 { font-size: 20px; font-weight: 600; margin: 0 0 4px; }
.subtitle { color: var(--text-secondary); font-size: 13px; margin: 0 0 24px; }
.tiles { display: flex; gap: 32px; margin: 0 0 28px; flex-wrap: wrap; }
.tile-value { font-size: 28px; font-weight: 600; letter-spacing: -0.02em; }
.tile-label { color: var(--text-secondary); font-size: 12px; text-transform: uppercase;
              letter-spacing: 0.04em; margin-top: 2px; }
table { border-collapse: collapse; width: 100%; }
th { text-align: left; font-size: 12px; font-weight: 600; color: var(--text-secondary);
     text-transform: uppercase; letter-spacing: 0.04em;
     padding: 0 12px 8px 0; border-bottom: 1px solid var(--border); }
th.num, td.num { text-align: right; }
td { padding: 10px 12px 10px 0; border-bottom: 1px solid var(--border);
     font-variant-numeric: tabular-nums; }
td.name { font-weight: 500; }
.bar-cell { width: 42%; padding-right: 0; }
.bar-track { background: var(--surface-2); border-radius: 2px; height: 14px; width: 100%; }
.bar-fill { background: var(--series-1); height: 14px;
            border-radius: 0 4px 4px 0; min-width: 2px; }
.file-heading { font-size: 13px; font-weight: 600; color: var(--text-secondary);
                margin: 28px 0 10px; word-break: break-all; }
.caption { color: var(--text-muted); font-size: 12px; margin: 8px 0 0; }
.chip { display: inline-block; width: 10px; height: 10px; border-radius: 2px;
        margin-right: 8px; vertical-align: baseline;
        box-shadow: 0 0 0 1px rgba(128,128,128,0.35); }
footer { margin-top: 32px; padding-top: 16px; border-top: 1px solid var(--border);
         color: var(--text-muted); font-size: 12px; }
footer p { margin: 4px 0; }
.empty { color: var(--text-muted); font-style: italic; }
"""

DISCLAIMER = (
    "Volumes are computed from voxel counts and image spacing. "
    "Research and decision support only &mdash; not a medical device, not for diagnostic use."
)


def render_html(report: dict) -> str:
    model = html.escape(report.get("model", "unknown"))
    context_bits = [f"Model <strong>{model}</strong>"]
    if report.get("session"):
        context_bits.append(f"session <strong>{html.escape(report['session'])}</strong>")
    if report.get("scan"):
        context_bits.append(f"scan <strong>{html.escape(report['scan'])}</strong>")
    context_bits.append(f"generated {html.escape(report['generated'])}")

    total_volume = sum(result["total_volume_ml"] for result in report["results"])
    total_structures = sum(len(result["structures"]) for result in report["results"])

    parts = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Segmentation volumes</title>",
        f"<style>{CSS}</style></head><body>",
        '<div class="viz-root">',
        "<h1>Segmentation volumes</h1>",
        f'<p class="subtitle">{" &middot; ".join(context_bits)}</p>',
        '<div class="tiles">',
        f'<div><div class="tile-value">{total_volume:,.1f} mL</div>'
        '<div class="tile-label">Total segmented volume</div></div>',
        f'<div><div class="tile-value">{total_structures}</div>'
        '<div class="tile-label">Structures found</div></div>',
        "</div>",
    ]

    show_headings = len(report["results"]) > 1
    for result in report["results"]:
        if show_headings:
            parts.append(f'<p class="file-heading">{html.escape(result["file"])}</p>')
        if not result["structures"]:
            parts.append('<p class="empty">No labelled structures in this mask.</p>')
            continue

        largest = max(item["volume_ml"] for item in result["structures"]) or 1.0
        parts.append(
            "<table><thead><tr>"
            "<th>Structure</th><th class='num'>Volume (mL)</th>"
            "<th class='num'>Voxels</th><th class='num'>Share</th>"
            "<th class='bar-cell'></th>"
            "</tr></thead><tbody>"
        )
        for item in result["structures"]:
            share = item["volume_ml"] / result["total_volume_ml"] * 100 if result["total_volume_ml"] else 0.0
            width = max(item["volume_ml"] / largest * 100, 0.5)
            title = html.escape(f"{item['name']}: {item['volume_ml']:,.2f} mL")
            red, green, blue = label_color(item["label"])
            chip = f"<span class='chip' style='background:rgb({red},{green},{blue})'></span>"
            parts.append(
                f"<tr><td class='name'>{chip}{html.escape(item['name'])}</td>"
                f"<td class='num'>{item['volume_ml']:,.2f}</td>"
                f"<td class='num'>{item['voxels']:,}</td>"
                f"<td class='num'>{share:.1f}%</td>"
                f"<td class='bar-cell'><div class='bar-track' title='{title}'>"
                f"<div class='bar-fill' style='width:{width:.1f}%'></div></div></td></tr>"
            )
        parts.append("</tbody></table>")

        voxel = " &times; ".join(f"{size:g}" for size in result["voxel_size_mm"])
        matrix = " &times; ".join(str(dimension) for dimension in result["shape"])
        parts.append(f'<p class="caption">Voxel size {voxel} mm &middot; matrix {matrix}</p>')

    parts.append(f"<footer><p>{DISCLAIMER}</p></footer>")
    parts.append("</div></body></html>")
    return "\n".join(parts)
