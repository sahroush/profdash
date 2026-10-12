"""CS research areas, as defined by CSRankings.

Four groups, 27 areas. Each area lists the conferences CSRankings counts
toward it; a professor's area profile is the sum of their (author-adjusted)
paper counts per area over those conferences.

`next_tier` conferences are shown by CSRankings only on request, but they
are still real signal for "what does this person work on", so profdash
counts them.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Area:
    code: str            # CSRankings parent-area code (stored in professor_areas)
    title: str           # display name (stored in professors.area)
    group: str           # one of GROUPS
    confs: tuple[str, ...]
    next_tier: tuple[str, ...] = ()

    @property
    def all_confs(self) -> tuple[str, ...]:
        return self.confs + self.next_tier


GROUPS: tuple[str, ...] = ("AI", "Systems", "Theory", "Interdisciplinary")

AREAS: tuple[Area, ...] = (
    # --- AI -----------------------------------------------------------------
    Area("ai", "AI", "AI", ("aaai", "ijcai")),
    Area("vision", "Vision", "AI", ("cvpr", "eccv", "iccv")),
    Area("mlmining", "ML", "AI", ("icml", "iclr", "nips"), ("kdd",)),
    Area("nlp", "NLP", "AI", ("acl", "emnlp", "naacl")),
    Area("inforet", "Web+IR", "AI", ("sigir", "www")),
    # --- Systems ------------------------------------------------------------
    Area("arch", "Architecture", "Systems", ("asplos", "isca", "micro"), ("hpca",)),
    Area("comm", "Networks", "Systems", ("sigcomm", "nsdi")),
    Area("sec", "Security", "Systems", ("ccs", "oakland", "usenixsec"), ("ndss",)),
    Area("mod", "Databases", "Systems", ("sigmod", "vldb"), ("icde", "pods")),
    Area("da", "EDA", "Systems", ("dac", "iccad")),
    Area("bed", "Embedded", "Systems", ("emsoft", "rtas", "rtss")),
    Area("hpc", "HPC", "Systems", ("sc", "hpdc", "ics")),
    Area("mobile", "Mobile", "Systems", ("mobicom", "mobisys", "sensys")),
    Area("metrics", "Metrics", "Systems", ("imc", "sigmetrics")),
    Area("ops", "OS", "Systems", ("sosp", "osdi"), ("eurosys", "fast", "usenixatc")),
    Area("plan", "PL", "Systems", ("popl", "pldi"), ("icfp", "oopsla")),
    Area("soft", "SE", "Systems", ("fse", "icse"), ("ase", "issta")),
    # --- Theory -------------------------------------------------------------
    Area("act", "Algorithms", "Theory", ("focs", "soda", "stoc")),
    Area("crypt", "Crypto", "Theory", ("crypto", "eurocrypt")),
    Area("log", "Logic", "Theory", ("cav", "lics")),
    # --- Interdisciplinary --------------------------------------------------
    Area("bio", "Comp. Bio", "Interdisciplinary", ("ismb", "recomb")),
    Area("graph", "Graphics", "Interdisciplinary",
         ("siggraph", "siggraph-asia"), ("eurographics",)),
    Area("csed", "CS Education", "Interdisciplinary", ("sigcse",)),
    Area("ecom", "Econ+Comp", "Interdisciplinary", ("ec", "wine")),
    Area("chi", "HCI", "Interdisciplinary", ("chiconf", "ubicomp", "uist")),
    Area("robotics", "Robotics", "Interdisciplinary", ("icra", "iros", "rss")),
    Area("visualization", "Visualization", "Interdisciplinary", ("vis", "vr")),
)

BY_CODE: dict[str, Area] = {a.code: a for a in AREAS}
CONF_TO_AREA: dict[str, str] = {c: a.code for a in AREAS for c in a.all_confs}
_TITLE_TO_CODE = {a.title.lower(): a.code for a in AREAS}


def areas_in_group(group: str) -> list[Area]:
    return [a for a in AREAS if a.group.lower() == group.lower()]


def resolve(token: str) -> list[Area]:
    """Resolve a user-supplied token (area code, area title, or group name)
    to areas. A group is written `group:systems` (or just `systems`, except
    `ai`, which is also an area code). Raises ValueError listing valid choices when it matches nothing."""
    t = token.strip().lower()
    if t.startswith("group:"):          # "ai" alone means the AI *area*
        grp = areas_in_group(t[6:])
        if grp:
            return grp
    elif t in BY_CODE:
        return [BY_CODE[t]]
    if t in _TITLE_TO_CODE:
        return [BY_CODE[_TITLE_TO_CODE[t]]]
    grp = areas_in_group(t)
    if grp:
        return grp
    valid = ", ".join(sorted(BY_CODE) + [f"group:{g.lower()}" for g in GROUPS])
    raise ValueError(f"unknown area {token!r}; valid: {valid}")


def primary_area(scores: dict[str, float]) -> Area | None:
    """Area with the highest paper score (ties broken by AREAS order)."""
    best, best_v = None, 0.0
    for a in AREAS:
        v = scores.get(a.code, 0.0)
        if v > best_v:
            best, best_v = a, v
    return best
