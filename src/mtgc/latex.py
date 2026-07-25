"""Génération des catalogues PDF (LuaLaTeX).

Port du frontend de la v1, avec quatre corrections structurantes :

* les données viennent de SQLite, plus d'un JSON aplati ;
* tri naturel ``(préfixe, entier, suffixe)`` au lieu du ``zfill()`` qui
  classait ``"00012"`` avant ``"003"`` ;
* les cartes multi-faces sont rendues depuis ``card_faces`` — on ne fabrique
  plus de faux collector numbers ``123a``/``123b`` qui corrompaient la base ;
* échappement LaTeX systématique (la v1 ne traitait que ``_``).
"""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Config
from .util import get_logger

log = get_logger("latex")

CARDS_PER_ROW = 3
ROWS_PER_PAGE = 3
CARDS_PER_PAGE = CARDS_PER_ROW * ROWS_PER_PAGE

_LATEX_ESCAPES = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
    "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}


def esc(text) -> str:
    """Échappe une chaîne pour LaTeX."""
    if text is None:
        return ""
    out = []
    for ch in str(text):
        out.append(_LATEX_ESCAPES.get(ch, ch))
    return "".join(out)


@dataclass
class Volume:
    index: int
    total: int
    set_codes: list[str]
    title: str


# ------------------------------------------------------------- préambule

def has_inkscape() -> bool:
    """Le package LaTeX ``svg`` délègue la conversion à Inkscape. Sans lui, on
    se passe des icônes de set plutôt que de faire échouer la compilation."""
    return shutil.which("inkscape") is not None


def _preamble(cfg: Config, title: str, subtitle: str) -> str:
    static = cfg.static_dir
    bg = static / "bg.jpg"
    fonts = static / "fonts"
    svg_block = "\\usepackage{svg}\n" if has_inkscape() else ""
    bg_block = ""
    if bg.exists():
        bg_block = f"""
\\usepackage[pages=all]{{background}}
\\backgroundsetup{{
  scale=1, color=black, opacity=0.4, angle=0,
  contents={{\\includegraphics[width=\\paperwidth,height=\\paperheight]{{{bg}}}}}
}}
"""
    font_block = ""
    if (fonts / "Beleren2016-Bold.ttf").exists():
        font_block = (f"\\setmainfont{{Beleren2016-Bold.ttf}}[Path={fonts}/]\n")

    return f"""\\documentclass[a4paper,11pt,twoside]{{article}}
\\usepackage{{graphicx}}
\\usepackage{{tabularx}}
\\usepackage{{longtable}}
\\usepackage[a4paper,left=12mm,right=12mm,top=16mm,bottom=18mm]{{geometry}}
\\usepackage{{fontspec}}
\\usepackage{{fancyhdr}}
{svg_block}\\usepackage[colorlinks=true,linkcolor=blue,urlcolor=blue,
            bookmarks=true,bookmarksopen=true,bookmarksnumbered=true]{{hyperref}}
\\renewcommand*\\contentsname{{Liste des éditions}}
{bg_block}{font_block}
\\setlength{{\\tabcolsep}}{{1.2mm}}
\\pagestyle{{fancy}}
\\fancyhf{{}}
\\fancyhead[LE,RO]{{\\small {esc(subtitle)}}}
\\fancyfoot[LE,RO]{{\\small \\leftmark}}
\\fancyfoot[RE,LO]{{\\small Page \\thepage}}
\\renewcommand{{\\headrulewidth}}{{0.2pt}}

\\title{{{esc(title)}}}
\\author{{Généré par MTGcyCLAUDEpedia — données et images \\href{{https://scryfall.com}}{{Scryfall}}}}
\\date{{{datetime.now().strftime('%d/%m/%Y')}}}

\\begin{{document}}
\\maketitle
\\thispagestyle{{empty}}
\\newpage
\\tableofcontents
\\newpage
"""


# --------------------------------------------------------------- données

def _fetch_sets(conn: sqlite3.Connection, codes: list[str]) -> list[sqlite3.Row]:
    q = ",".join("?" * len(codes))
    rows = conn.execute(
        f"SELECT * FROM sets WHERE code IN ({q}) ORDER BY released_at, code",
        codes).fetchall()
    return rows


def _fetch_images(conn: sqlite3.Connection, set_code: str,
                  unique_art_only: bool) -> list[tuple[str, str]]:
    """Renvoie ``[(légende, chemin_image)]`` triés naturellement."""
    extra = "AND c.is_unique_art = 1" if unique_art_only else ""
    rows = conn.execute(f"""
        SELECT c.name, c.collector_number, c.cn_prefix, c.cn_num, c.cn_suffix,
               i.face_index, i.path
        FROM cards c
        JOIN images i ON i.card_id = c.id
        WHERE c.set_code = ? {extra}
        ORDER BY c.cn_prefix, c.cn_num, c.cn_suffix, i.face_index
    """, (set_code,)).fetchall()
    out = []
    for r in rows:
        if not Path(r["path"]).exists():
            continue
        label = r["name"] or ""
        if r["face_index"]:
            label += f" (face {r['face_index'] + 1})"
        out.append((label, r["path"]))
    return out


# --------------------------------------------------------------- rendu

def _set_header(s: sqlite3.Row, n_images: int, cfg: Config) -> str:
    icon = Path(s["icon_path"]) if s["icon_path"] else None
    icon_tex = ""
    if icon and icon.exists() and has_inkscape():
        stem = str(icon.with_suffix(""))
        icon_tex = (f"\\fancyhead[RE,LO]{{\\includesvg[height=6mm]{{{stem}}}}}\n")
    else:
        icon_tex = (f"\\fancyhead[RE,LO]{{\\small {esc((s['code'] or '').upper())}}}\n")

    rows = [
        ("Nom", esc(s["name"])),
        ("Code", esc((s["code"] or "").upper())),
        ("Sortie", esc(s["released_at"])),
        ("Type", esc(s["set_type"])),
        ("Cartes annoncées", esc(s["card_count"])),
        ("Images présentes", str(n_images)),
    ]
    if s["block"]:
        rows.append(("Bloc", esc(s["block"])))
    if s["parent_set_code"]:
        rows.append(("Set parent", esc(s["parent_set_code"].upper())))
    if s["digital"]:
        rows.append(("Support", "numérique uniquement"))

    body = " \\\\\n".join(f"\\textbf{{{k}}} & {v}" for k, v in rows)
    title = f"{(s['code'] or '').upper()} — {s['name']}"
    return (f"\n\\section{{{esc(title)}}}\n{icon_tex}"
            f"\\begin{{tabular}}{{@{{}}ll@{{}}}}\n{body} \\\\\n\\end{{tabular}}\n"
            f"\\vspace{{4mm}}\n\n")


def _grid(images: list[tuple[str, str]], placeholder: Path | None) -> str:
    """Grille 3×3 par page, complétée par le placeholder transparent."""
    if not images:
        return "\\emph{Aucune image disponible pour cette édition.}\n\\newpage\n"

    cells = [path for _, path in images]
    if placeholder and placeholder.exists():
        while len(cells) % CARDS_PER_PAGE != 0:
            cells.append(str(placeholder))

    out = []
    for page_start in range(0, len(cells), CARDS_PER_PAGE):
        page = cells[page_start:page_start + CARDS_PER_PAGE]
        out.append("\\begin{center}\n")
        out.append("\\begin{tabular}{ccc}\n")
        for row_start in range(0, len(page), CARDS_PER_ROW):
            row = page[row_start:row_start + CARDS_PER_ROW]
            while len(row) < CARDS_PER_ROW:
                row.append(None)
            tex_cells = [
                (f"\\includegraphics[width=58mm]{{{c}}}" if c else "")
                for c in row
            ]
            out.append(" & ".join(tex_cells) + " \\\\\n")
        out.append("\\end{tabular}\n\\end{center}\n\\newpage\n")
    return "".join(out)


def _artist_index(conn: sqlite3.Connection, codes: list[str]) -> str:
    q = ",".join("?" * len(codes))
    rows = conn.execute(f"""
        SELECT artist, name, set_code, collector_number
        FROM cards
        WHERE set_code IN ({q}) AND artist IS NOT NULL AND artist <> ''
        ORDER BY artist COLLATE NOCASE, name COLLATE NOCASE
    """, codes).fetchall()
    if not rows:
        return ""
    out = ["\n\\section{Index des illustrateurs}\n",
           "\\begin{longtable}{@{}p{55mm}p{75mm}p{25mm}@{}}\n",
           "\\textbf{Illustrateur} & \\textbf{Carte} & \\textbf{Édition} \\\\\n",
           "\\hline\n\\endhead\n"]
    last = None
    for r in rows:
        artist = esc(r["artist"]) if r["artist"] != last else ""
        last = r["artist"]
        out.append(f"{artist} & {esc(r['name'])} & "
                   f"{esc((r['set_code'] or '').upper())} {esc(r['collector_number'])} \\\\\n")
    out.append("\\end{longtable}\n")
    return "".join(out)


# ------------------------------------------------------------- volumes

def plan_volumes(conn: sqlite3.Connection, cfg: Config, *,
                 sets: list[str] | None = None,
                 unique_art_only: bool = False) -> list[Volume]:
    """Découpe l'ensemble des sets en volumes d'environ ``cards_per_volume``
    images, en respectant l'ordre chronologique de sortie."""
    extra = "AND c.is_unique_art = 1" if unique_art_only else ""
    where = ""
    params: list = []
    if sets:
        where = "AND s.code IN (%s)" % ",".join("?" * len(sets))
        params = [s.lower() for s in sets]
    rows = conn.execute(f"""
        SELECT s.code, s.released_at, COUNT(i.card_id) AS n
        FROM sets s
        JOIN cards c ON c.set_code = s.code {extra}
        JOIN images i ON i.card_id = c.id
        WHERE 1=1 {where}
        GROUP BY s.code
        HAVING n > 0
        ORDER BY s.released_at, s.code
    """, params).fetchall()

    volumes: list[list[str]] = []
    current: list[str] = []
    count = 0
    for r in rows:
        if current and count + r["n"] > cfg.cards_per_volume:
            volumes.append(current)
            current, count = [], 0
        current.append(r["code"])
        count += r["n"]
    if current:
        volumes.append(current)

    total = len(volumes)
    return [
        Volume(i + 1, total, codes,
               f"MTG Card Catalog — volume {i + 1}/{total} "
               f"({codes[0].upper()} → {codes[-1].upper()})")
        for i, codes in enumerate(volumes)
    ]


def render_volume(conn: sqlite3.Connection, cfg: Config, vol: Volume, *,
                  unique_art_only: bool = False,
                  with_artist_index: bool = True) -> Path:
    placeholder = cfg.static_dir / "PHalpha.png"
    parts = [_preamble(cfg, vol.title, f"MTG Card Catalog {vol.index}/{vol.total}")]

    for s in _fetch_sets(conn, vol.set_codes):
        images = _fetch_images(conn, s["code"], unique_art_only)
        parts.append(_set_header(s, len(images), cfg))
        parts.append(_grid(images, placeholder))

    if with_artist_index:
        parts.append(_artist_index(conn, vol.set_codes))

    parts.append("\n\\end{document}\n")

    cfg.build_dir.mkdir(parents=True, exist_ok=True)
    tex_path = cfg.build_dir / f"catalog-vol{vol.index:02d}.tex"
    tex_path.write_text("".join(parts), encoding="utf-8")
    log.info("volume %d/%d écrit : %s (%d sets)",
             vol.index, vol.total, tex_path.name, len(vol.set_codes))
    return tex_path


def compile_tex(cfg: Config, tex_path: Path) -> Path | None:
    """Compile deux fois (table des matières) puis déplace le PDF."""
    engine = cfg.latex_engine
    if shutil.which(engine) is None:
        log.error("%s introuvable — installe texlive (lualatex, package svg, "
                  "inkscape pour les icônes SVG)", engine)
        return None
    cmd = [engine, "--shell-escape", "-interaction=nonstopmode",
           "-halt-on-error", tex_path.name]
    for run in (1, 2):
        proc = subprocess.run(cmd, cwd=tex_path.parent,
                              capture_output=True, text=True)
        if proc.returncode != 0:
            log.error("échec %s (passe %d) — voir %s",
                      engine, run, tex_path.with_suffix(".log"))
            tail = "\n".join(proc.stdout.splitlines()[-25:])
            log.error("dernières lignes :\n%s", tail)
            return None
    pdf = tex_path.with_suffix(".pdf")
    if not pdf.exists():
        log.error("aucun PDF produit pour %s", tex_path.name)
        return None
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    dest = cfg.out_dir / pdf.name
    shutil.move(str(pdf), dest)
    size = dest.stat().st_size
    log.info("PDF généré : %s (%.1f Mio)", dest, size / (1 << 20))
    return dest


def build_catalog(conn: sqlite3.Connection, cfg: Config, *,
                  sets: list[str] | None = None,
                  unique_art_only: bool = False,
                  volumes: list[int] | None = None,
                  compile_pdf: bool = True) -> list[Path]:
    plan = plan_volumes(conn, cfg, sets=sets, unique_art_only=unique_art_only)
    if not plan:
        log.warning("aucune image en base — lance d'abord « mtgc images »")
        return []
    log.info("%d volume(s) planifié(s)", len(plan))
    produced = []
    for vol in plan:
        if volumes and vol.index not in volumes:
            continue
        tex = render_volume(conn, cfg, vol, unique_art_only=unique_art_only)
        if compile_pdf:
            pdf = compile_tex(cfg, tex)
            if pdf:
                produced.append(pdf)
        else:
            produced.append(tex)
    return produced
