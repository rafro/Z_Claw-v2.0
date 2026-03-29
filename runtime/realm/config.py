"""
Realm Layer — Single Source of Truth.

All world data: division config, commander identities, rank ladders,
skill XP values, soldier names, order vocabulary, achievement definitions,
base rank table, XP-per-level curve, and event templates.

xp.py, server.js (via /mobile/api/realm/config endpoint), and
mobile/index.html all derive from this single registry.

To add a new skill:   add entry under the division's "skills" dict.
To add a new division: add a new top-level key to DIVISIONS.
To change a rank name: edit the "ranks" list for that division.
Nothing else needs touching.
"""

# ── J_Claw identity ───────────────────────────────────────────────────────────

JCLAW_IDENTITY = {
    "name":   "J_Claw",
    "role":   "Matthew's armored command avatar and executive voice inside the realm",
    "tagline": "The commander is online. The realm is active.",
    "color":  "#89b4fa",   # Catppuccin blue
    "glow":   "#89b4fa33",
}

# ── XP curve (base level progression) ─────────────────────────────────────────
# XP required to advance FROM each level. Index = level.
# Level 10+: 2100 × 1.3^(level-9)
XP_PER_LEVEL = [0, 100, 180, 300, 450, 650, 900, 1200, 1600, 2100]

# ── Base ranks (global, driven by level) ──────────────────────────────────────
BASE_RANKS = [
    {"min_level": 50,  "title": "The Eternal Orchestrator"},
    {"min_level": 35,  "title": "Grand Sovereign"},
    {"min_level": 20,  "title": "Warlord of Automation"},
    {"min_level": 10,  "title": "Commander of the Realm"},
    {"min_level": 5,   "title": "Keeper of Systems"},
    {"min_level": 1,   "title": "Apprentice of the Realm"},
]

# Base XP auto-granted when a division crosses a rank threshold.
# Preserves sovereignty: Matthew's /reward grants are still the primary driver
# at higher levels. Division rank-ups reward consistent operational excellence.
RANK_UP_BASE_XP = {
    1: 15,   # Tier 1 (51 XP)  — "Order achieves first ascension"
    2: 25,   # Tier 2 (151 XP) — "Order reaches expertise"
    3: 40,   # Tier 3 (301 XP) — "Order approaches mastery"
    4: 60,   # Tier 4 (500 XP) — "Order achieves legendary status"
}

# ── Division thresholds (shared by all divisions) ─────────────────────────────
DIV_XP_THRESHOLDS = [0, 51, 151, 301, 500]

# ── Divisions — the canonical registry ────────────────────────────────────────
#
# Each division entry:
#   key          str  — internal key matching jclaw-stats.json
#   commander    str  — true name
#   title        str  — full formal title
#   order        str  — order name
#   color        str  — hex color
#   glow         str  — hex with alpha for glow effect
#   lore         str  — character lore (1–2 sentences)
#   ranks        list — 5 rank titles matching DIV_XP_THRESHOLDS [0,51,151,301,500]
#   abilities    list — {name, desc} shown on character sheet
#   skills       dict — skill_key: {xp, soldier, icon, anim, label}
#   vocabulary   dict — division-specific language for events/states
#   sprite_theme str  — visual identity descriptor for pixel art direction

DIVISIONS = {

    "opportunity": {
        "key":       "opportunity",
        "commander": "VAEL",
        "title":     "Vael, Spear of the Dawnhunt",
        "order":     "The Dawnhunt Order",
        "color":     "#f59e0b",
        "glow":      "#f59e0b40",
        "lore": (
            "The realm's most relentless tracker. Vael ranges ahead of all others — "
            "marking targets, cutting false leads, and never returning to the field "
            "empty-handed. Where the Dawnhunt rides, the quarry has nowhere left to run."
        ),
        "ranks": [
            "Scout of the Dawnhunt",   # 0   XP
            "Vanguard Pathfinder",      # 51  XP
            "Grand Hunter",             # 151 XP
            "Sovereign Tracker",        # 301 XP
            "Spear of the Hunt",        # 500 XP
        ],
        "abilities": [
            {"name": "Mark the Quarry",  "desc": "Scans the field for new targets and marks them for pursuit"},
            {"name": "The Arbiter's Cut","desc": "Cuts weak leads from strong — only worthy targets survive the filter"},
            {"name": "Strike the Vein",  "desc": "Uncovers hidden funding potential in every opportunity"},
        ],
        "skills": {
            "job-intake":     {"xp": 10, "soldier": "The Tracker",    "icon": "⟶", "anim": "arrow",  "label": "Mark the Quarry"},
            "hard-filter":    {"xp":  5, "soldier": "The Arbiter",    "icon": "⟁", "anim": "slash",  "label": "The Arbiter's Cut"},
            "funding-finder": {"xp":  5, "soldier": "The Prospector", "icon": "◈", "anim": "scan",   "label": "Strike the Vein"},
        },
        "vocabulary": {
            "quest":       "on the hunt",
            "rest":        "tracking cold",
            "standby":     "awaiting quarry",
            "wound":       "cold trail",
            "hydrate":     "called to the field",
            "rank_up":     "Vael advances the hunt",
            "streak_hold": "the hunt continues unbroken",
            "streak_lost": "the trail has gone cold",
        },
        "sprite_theme": "Light scout armor with pointed hood. Recurve bow slung on back. Amber glow at eye line. Streamlined mobile silhouette.",
    },

    "trading": {
        "key":       "trading",
        "commander": "SEREN",
        "title":     "Seren, Oracle of the Auric Veil",
        "order":     "The Auric Veil",
        "color":     "#06b6d4",
        "glow":      "#06b6d440",
        "lore": (
            "Still as deep water, sharp as a blade. Seren reads signals that others cannot "
            "see and speaks verdicts the market has no choice but to confirm. "
            "The Auric Veil has never missed a pattern that mattered."
        ),
        "ranks": [
            "Signal Initiate",          # 0   XP
            "Veil Adept",               # 51  XP
            "Pattern Seer",             # 151 XP
            "Voice of the Oracle",      # 301 XP
            "Grand Oracle of Markets",  # 500 XP
        ],
        "abilities": [
            {"name": "Read the Runes",    "desc": "Reads market signals and conditions across all channels"},
            {"name": "The Oracle's Edict","desc": "Delivers the daily market verdict — the Veil speaks what the field confirms"},
            {"name": "Shadow Run",        "desc": "Tests strategy in the shadows without risking real capital"},
            {"name": "Pattern Lock",      "desc": "Backtests signal patterns against historical data to find true edge"},
        ],
        "skills": {
            "trading-report":  {"xp": 15, "soldier": "The Seer's Voice",    "icon": "◉", "anim": "slash",   "label": "The Oracle's Edict"},
            "market-scan":     {"xp":  5, "soldier": "The Signal Keeper",   "icon": "◈", "anim": "scan",    "label": "Read the Runes"},
            "virtual-trader":  {"xp":  8, "soldier": "The Shadow Runner",   "icon": "⟁", "anim": "slash",   "label": "Shadow Run"},
            "backtester":      {"xp":  5, "soldier": "The Pattern Keeper",  "icon": "◫", "anim": "circuit", "label": "Pattern Lock"},
        },
        "vocabulary": {
            "quest":       "reading the veil",
            "rest":        "signals quiet",
            "standby":     "awaiting the pattern",
            "wound":       "clouded reading",
            "hydrate":     "runes reopened",
            "rank_up":     "Seren speaks a new truth",
            "streak_hold": "the pattern holds unbroken",
            "streak_lost": "the signal has gone dark",
        },
        "sprite_theme": "Tall flowing oracle robes with narrow peaked hat. Staff in right hand topped with data-orb. Cyan glow at eyes. Robe widens toward base.",
    },

    "dev_automation": {
        "key":       "dev_automation",
        "commander": "KAELEN",
        "title":     "Kaelen, Warden of the Iron Codex",
        "order":     "The Iron Codex",
        "color":     "#a78bfa",
        "glow":      "#a78bfa40",
        "lore": (
            "The realm's master builder. Kaelen forges the architecture that keeps everything "
            "else standing — repairing what is broken, automating what is slow, and constructing "
            "what does not yet exist. The Iron Codex never rests."
        ),
        "ranks": [
            "Codex Initiate",           # 0   XP
            "Iron Smith",               # 51  XP
            "Forge Warden",             # 151 XP
            "Codex Architect",          # 301 XP
            "Master of the Iron Codex", # 500 XP
        ],
        "abilities": [
            {"name": "Watch the Forge",     "desc": "Guards repositories for drift, rot, and unmerged chaos"},
            {"name": "Reforge",             "desc": "Hunts dead code and complexity — strips it clean and rebuilds"},
            {"name": "Codex Report",        "desc": "Delivers the weekly build, automation, and system integrity digest"},
            {"name": "Temper the Pipeline", "desc": "Manages build artifacts, packages, and pipeline integrity"},
            {"name": "Inscribe",            "desc": "Updates documentation and code annotations across the realm"},
        ],
        "skills": {
            "repo-monitor":     {"xp": 10, "soldier": "The Warden",           "icon": "⬡", "anim": "circuit", "label": "Watch the Forge"},
            "refactor-scan":    {"xp":  5, "soldier": "The Reforger",         "icon": "⟁", "anim": "circuit", "label": "Reforge"},
            "doc-update":       {"xp":  5, "soldier": "The Scribe",           "icon": "◫", "anim": "circuit", "label": "Inscribe"},
            "debug-agent":      {"xp":  8, "soldier": "The Debugger",         "icon": "◈", "anim": "circuit", "label": "Debug the Construct"},
            "artifact-manager": {"xp":  3, "soldier": "The Relic Keeper",     "icon": "⬡", "anim": "circuit", "label": "Temper the Pipeline"},
            "dev-digest":       {"xp":  5, "soldier": "The Chronicler",       "icon": "◉", "anim": "circuit", "label": "Codex Report"},
            "dev-pipeline":     {"xp": 10, "soldier": "The Architect",        "icon": "⬢", "anim": "circuit", "label": "Lay the Foundation"},
        },
        "vocabulary": {
            "quest":       "forging",
            "rest":        "forge cooled",
            "standby":     "awaiting the blueprint",
            "wound":       "fractured construct",
            "hydrate":     "forge re-lit",
            "rank_up":     "Kaelen expands the Codex",
            "streak_hold": "the forge burns unbroken",
            "streak_lost": "the forge has gone cold",
        },
        "sprite_theme": "Heavy forge plate with industrial visor/goggle set. Wrench in right hand. Circuit line accent across chest. Deep purple with violet glow.",
    },

    "personal": {
        "key":       "personal",
        "commander": "LYRIN",
        "title":     "Lyrin, Keeper of the Ember Covenant",
        "order":     "The Ember Covenant",
        "color":     "#10b981",
        "glow":      "#10b98140",
        "lore": (
            "The heart of the realm. Without Lyrin's tending, the sovereign's flame grows "
            "cold and everything else follows. The Ember Covenant exists for one reason: "
            "to ensure the commander never burns out."
        ),
        "ranks": [
            "Covenant Initiate",    # 0   XP
            "Flame Tender",         # 51  XP
            "Guardian of Vitality", # 151 XP
            "Covenant Warden",      # 301 XP
            "Eternal Keeper",       # 500 XP
        ],
        "abilities": [
            {"name": "Tend the Flame",  "desc": "Logs daily health and habits — the Covenant's most sacred duty"},
            {"name": "Read the Ashes",  "desc": "Watches for signs of burnout before they spread beyond control"},
            {"name": "Inner Sight",     "desc": "Connects health patterns to performance and operational output"},
            {"name": "The Covenant's Voice", "desc": "Delivers the weekly personal health and vitality digest"},
        ],
        "skills": {
            "health-logger":    {"xp": 15, "soldier": "The Tender",   "icon": "◉", "anim": "sparkle", "label": "Tend the Flame"},
            "perf-correlation": {"xp": 10, "soldier": "The Lens",     "icon": "◈", "anim": "sparkle", "label": "Inner Sight"},
            "burnout-monitor":  {"xp":  5, "soldier": "The Watchfire","icon": "⟁", "anim": "sparkle", "label": "Read the Ashes"},
            "personal-digest":  {"xp":  5, "soldier": "The Voice",    "icon": "◫", "anim": "sparkle", "label": "The Covenant's Voice"},
        },
        "vocabulary": {
            "quest":       "tending the flame",
            "rest":        "flame at rest",
            "standby":     "covenant watching",
            "wound":       "dimmed flame",
            "hydrate":     "flame rekindled",
            "rank_up":     "Lyrin deepens the Covenant",
            "streak_hold": "the flame burns unbroken",
            "streak_lost": "the flame has dimmed",
        },
        "sprite_theme": "Flowing healer robe with leaf-crown motif. Small healing orb in left hand radiating green light. Warm grounded silhouette.",
    },

    "op_sec": {
        "key":       "op_sec",
        "commander": "ZETH",
        "title":     "Zeth, Veilkeeper of the Nullward Circle",
        "order":     "The Nullward Circle",
        "color":     "#ef4444",
        "glow":      "#ef444440",
        "lore": (
            "The Nullward Circle sees what others do not. Zeth moves through the spaces "
            "between systems — sealing breaches before they compound, watching the "
            "perimeter that everyone else forgets exists. Nothing passes the veil unseen."
        ),
        "ranks": [
            "Circle Watchman",       # 0   XP
            "Veil Scout",            # 51  XP
            "Shadow Warden",         # 151 XP
            "Grand Sentinel",        # 301 XP
            "Sovereign of the Null", # 500 XP
        ],
        "abilities": [
            {"name": "Inspect the Veil",  "desc": "Checks device security posture from the shadows"},
            {"name": "Map the Dark",      "desc": "Surveys the full threat surface of the realm"},
            {"name": "Breach Watch",      "desc": "Scans for known breaches before they compound into crises"},
            {"name": "Credential Sweep",  "desc": "Audits credential hygiene across all realm systems"},
            {"name": "Privacy Ward",      "desc": "Scans for PII exposure and privacy risk across the perimeter"},
            {"name": "Null Report",       "desc": "Delivers the weekly threat assessment and security posture brief"},
        ],
        "skills": {
            "device-posture":     {"xp": 10, "soldier": "The Posture Guard",       "icon": "⬡", "anim": "shield", "label": "Inspect the Veil"},
            "breach-check":       {"xp": 10, "soldier": "The Breach Scout",        "icon": "⟁", "anim": "shield", "label": "Breach Watch"},
            "threat-surface":     {"xp":  8, "soldier": "The Surface Warden",      "icon": "◈", "anim": "shield", "label": "Map the Dark"},
            "cred-audit":         {"xp":  8, "soldier": "The Credential Keeper",   "icon": "◫", "anim": "shield", "label": "Credential Sweep"},
            "privacy-scan":       {"xp":  5, "soldier": "The Privacy Warden",      "icon": "⬡", "anim": "shield", "label": "Privacy Ward"},
            "opsec-digest":       {"xp":  5, "soldier": "The Brief",               "icon": "◉", "anim": "shield", "label": "Null Report"},
            "mobile-audit-review":{"xp":  5, "soldier": "The Mobile Warden",       "icon": "◈", "anim": "shield", "label": "Audit the Mobile Veil"},
            "sentinel-health":    {"xp":  5, "soldier": "The Sentinel",            "icon": "⬢", "anim": "shield", "label": "Sentinel Watch"},
            "security-scan":      {"xp": 10, "soldier": "The Code Sentinel",       "icon": "⬡", "anim": "shield", "label": "Audit the Veil"},
        },
        "vocabulary": {
            "quest":       "watching the veil",
            "rest":        "perimeter quiet",
            "standby":     "circle at watch",
            "wound":       "breach in the veil",
            "hydrate":     "watch restored",
            "rank_up":     "Zeth seals a new layer",
            "streak_hold": "the watch holds unbroken",
            "streak_lost": "the watch has lapsed",
        },
        "sprite_theme": "Deep shadow cloak, full hood, no visible face. Only two small red eyes glow in darkness. Barely-visible blade at side. Most minimal silhouette — designed to feel like a void.",
    },

    "production": {
        "key":       "production",
        "commander": "LYKE",
        "title":     "Lyke, Architect of the Lykeon Forge",
        "order":     "The Lykeon Forge",
        "color":     "#f97316",
        "glow":      "#f9731640",
        "lore": (
            "An ancient craftwright who sees the blueprint beneath every chaos. "
            "Where others imagine, LYKE builds. Every asset in the realm passes "
            "through the Forge before it becomes real."
        ),
        "ranks": [
            "Apprentice of the Forge",          # 0   XP
            "Craftwright Adept",                 # 51  XP
            "Lykeon Architect",                  # 151 XP
            "Master of the Forge",               # 301 XP
            "Lyke, Architect of the Lykeon Forge", # 500 XP
        ],
        "abilities": [
            {"name": "Blueprint Vision",  "desc": "Sees the final form before a single stroke is made. Reduces prompt iterations."},
            {"name": "Forge Fire",        "desc": "Generates assets at accelerated pace during active production sprints."},
            {"name": "Continuity Lock",   "desc": "Ensures every asset in a set maintains visual coherence with its siblings."},
            {"name": "Quality Seal",      "desc": "No asset leaves the Forge without passing QA. Zero defects policy."},
            {"name": "Archive Mastery",   "desc": "The Forge catalog never forgets. Every asset is indexed and retrievable."},
        ],
        "skills": {
            "image-generate":     {"xp": 15, "soldier": "The Illustrator",    "icon": "🎨", "anim": "paint",    "label": "Forge the Vision"},
            "sprite-generate":    {"xp": 20, "soldier": "The Sprite Caster",  "icon": "⚔",  "anim": "flash",    "label": "Cast the Sprite"},
            "video-generate":     {"xp": 20, "soldier": "The Cinematist",     "icon": "🎬", "anim": "reel",     "label": "Roll the Reel"},
            "graphic-design":     {"xp": 15, "soldier": "The Draftsman",      "icon": "✏",  "anim": "draft",    "label": "Draft the Blueprint"},
            "prompt-craft":       {"xp":  5, "soldier": "The Lexicographer",  "icon": "📜", "anim": "scroll",   "label": "Write the Rune"},
            "style-check":        {"xp":  8, "soldier": "The Aesthete",       "icon": "👁",  "anim": "scan",     "label": "The Eye of the Forge"},
            "image-review":       {"xp":  8, "soldier": "The Inspector",      "icon": "🔍", "anim": "inspect",  "label": "The Inspector's Eye"},
            "audio-test":         {"xp":  8, "soldier": "The Resonant",       "icon": "🎵", "anim": "wave",     "label": "Tune the Resonance"},
            "video-review":       {"xp": 10, "soldier": "The Critic",         "icon": "🎞",  "anim": "review",   "label": "The Critic's Cut"},
            "asset-catalog":      {"xp":  5, "soldier": "The Archivist",      "icon": "📚", "anim": "catalog",  "label": "Seal the Archive"},
            "storyboard-compose": {"xp": 10, "soldier": "The Composer",       "icon": "🗺",  "anim": "compose",  "label": "Compose the Vision"},
            "continuity-check":   {"xp":  8, "soldier": "The Warden",         "icon": "⚖",  "anim": "balance",  "label": "Hold the Continuity"},
            "asset-deliver":      {"xp":  5, "soldier": "The Herald",         "icon": "📦", "anim": "deliver",  "label": "Deliver the Artifact"},
            "production-digest":  {"xp": 10, "soldier": "LYKE",               "icon": "⬡",  "anim": "forge",    "label": "The Forge Report"},
        },
        "vocabulary": {
            "quest":       "in production",
            "rest":        "forge cooling",
            "standby":     "awaiting blueprint",
            "wound":       "defective batch",
            "hydrate":     "restocking materials",
            "rank_up":     "LYKE expands the forge",
            "streak_hold": "the forge burns unbroken",
            "streak_lost": "the forge has gone cold",
        },
        "sprite_theme": "Armored architect in deep orange plate, glowing blueprint scrolls, forge-fire amber eyes, structural hexagonal motifs on armor. Confident builder stance.",
    },
}

# ── Achievements — data-driven, evaluated against stats ───────────────────────
#
# condition types:
#   division_xp_gt:   division[division] XP > value
#   any_division_xp_gte: any division XP >= value
#   base_level_gte:   level >= value
#   any_streak_gte:   any division's longest streak >= value
#   manual:           only granted via /api/reward (rulers_blessing)

ACHIEVEMENTS = [
    {
        "id":    "rulers_blessing",
        "icon":  "👑",
        "name":  "Ruler's Blessing",
        "desc":  "Recognized by Matthew, the sovereign of the realm",
        "condition": {"type": "manual"},
        "chronicle_lore": "Matthew has bestowed the Ruler's Blessing upon J_Claw — the first mark of sovereign recognition in the realm.",
    },
    {
        "id":    "first_hunt",
        "icon":  "🏹",
        "name":  "First Hunt",
        "desc":  "Vael marked the first quarry — the Dawnhunt Order has opened its ledger",
        "condition": {"type": "division_xp_gt", "division": "opportunity", "value": 0},
        "chronicle_lore": "Vael's Dawnhunt Order has made its first mark. The quarry board is open. The hunt has begun.",
    },
    {
        "id":    "market_watcher",
        "icon":  "📈",
        "name":  "First Signal",
        "desc":  "Seren read the first rune — the Auric Veil is active",
        "condition": {"type": "division_xp_gt", "division": "trading", "value": 0},
        "chronicle_lore": "Seren has spoken the first verdict. The Auric Veil is open and reading the field.",
    },
    {
        "id":    "code_warden",
        "icon":  "⬡",
        "name":  "Forge Lit",
        "desc":  "Kaelen lit the forge — the Iron Codex is building",
        "condition": {"type": "division_xp_gt", "division": "dev_automation", "value": 0},
        "chronicle_lore": "Kaelen has lit the Iron Codex forge for the first time. Construction of the realm has begun.",
    },
    {
        "id":    "covenant_keeper",
        "icon":  "🔥",
        "name":  "Flame Kindled",
        "desc":  "Lyrin tended the flame — the Ember Covenant is watching",
        "condition": {"type": "division_xp_gt", "division": "personal", "value": 0},
        "chronicle_lore": "Lyrin has kindled the Ember Covenant for the first time. The sovereign's flame is now tended.",
    },
    {
        "id":    "veil_opened",
        "icon":  "👁",
        "name":  "The Veil Opens",
        "desc":  "Zeth raised the first ward — the Nullward Circle is watching",
        "condition": {"type": "division_xp_gt", "division": "op_sec", "value": 0},
        "chronicle_lore": "Zeth has raised the first ward. The Nullward Circle is watching the perimeter.",
    },
    {
        "id":    "forge_lit",
        "icon":  "⬡",
        "name":  "The Forge Ignites",
        "desc":  "LYKE struck the first spark — the Lykeon Forge is open",
        "condition": {"type": "division_xp_gt", "division": "production", "value": 0},
        "chronicle_lore": "LYKE has struck the first spark. The Lykeon Forge is open and the first asset has entered production.",
    },
    {
        "id":    "loyal_flame",
        "icon":  "🔥",
        "name":  "Loyal Flame",
        "desc":  "Any order held a 7-day streak — morale reaches Elite",
        "condition": {"type": "any_streak_gte", "value": 7},
        "chronicle_lore": "An unbroken 7-day battle rhythm has been achieved. Elite morale status reached in the realm.",
    },
    {
        "id":    "division_master",
        "icon":  "⚔",
        "name":  "Division Master",
        "desc":  "An order reached the third ascension — mastery approaches",
        "condition": {"type": "any_division_xp_gte", "value": 301},
        "chronicle_lore": "An order of the realm has reached the third ascension. The path to mastery is clear.",
    },
    {
        "id":    "five_orders",
        "icon":  "✦",
        "name":  "Six Orders Stand",
        "desc":  "All six orders have opened their ledgers",
        "condition": {"type": "all_divisions_xp_gt", "value": 0},
        "chronicle_lore": "All six orders of the realm have opened their ledgers. The full command structure is active.",
    },
    {
        "id":    "realm_commander",
        "icon":  "🌟",
        "name":  "Realm Commander",
        "desc":  "J_Claw reached Level 10 — command authority established",
        "condition": {"type": "base_level_gte", "value": 10},
        "chronicle_lore": "J_Claw has reached Level 10 and claimed the rank of Commander of the Realm. The realm recognizes its commander.",
    },
    {
        "id":    "eternal",
        "icon":  "♾",
        "name":  "Eternal",
        "desc":  "J_Claw reached Level 50 — the Eternal Orchestrator ascends",
        "condition": {"type": "base_level_gte", "value": 50},
        "chronicle_lore": "J_Claw has transcended all known limits and claimed the eternal rank. The realm will not forget this moment.",
    },
    {
        "id":    "fortnight_flame",
        "icon":  "🔥",
        "name":  "Fortnight Flame",
        "desc":  "Any order held a 14-day streak — iron discipline proven",
        "condition": {"type": "any_streak_gte", "value": 14},
        "chronicle_lore": "Fourteen unbroken days of battle rhythm. The realm has witnessed true iron discipline.",
    },
    {
        "id":    "monthly_guardian",
        "icon":  "🛡",
        "name":  "Monthly Guardian",
        "desc":  "Any order held a 30-day streak — the guardian never rests",
        "condition": {"type": "any_streak_gte", "value": 30},
        "chronicle_lore": "Thirty days without lapse. The guardian's watch has become legend in the realm.",
    },
    {
        "id":    "first_prestige",
        "icon":  "✦",
        "name":  "First Prestige",
        "desc":  "J_Claw completed the first prestige cycle",
        "condition": {"type": "prestige_gte", "value": 1},
        "chronicle_lore": "The first cycle is complete. J_Claw has proven that mastery is not an end — it is a threshold.",
    },
    {
        "id":    "triple_prestige",
        "icon":  "✦✦✦",
        "name":  "Triple Prestige",
        "desc":  "J_Claw completed three prestige cycles",
        "condition": {"type": "prestige_gte", "value": 3},
        "chronicle_lore": "Three full cycles of mastery. The realm bends to a commander who has reset and rebuilt three times over.",
    },
    {
        "id":    "forge_ignited",
        "icon":  "🔨",
        "name":  "Forge Ignited",
        "desc":  "The Lykeon Forge has produced its first XP — production is live",
        "condition": {"type": "division_xp_gt", "division": "production", "value": 0},
        "chronicle_lore": "LYKE's forge has struck its first spark in the production line. The realm now has a maker.",
    },
]

# ── Chronicle event templates ─────────────────────────────────────────────────
# Used by chronicle.py to generate lore for automated events.
# {commander}, {order}, {rank}, {xp}, {streak}, {skill}, {soldier} are substituted.

CHRONICLE_TEMPLATES = {
    # Division rank-up tiers
    "rank_up_tier_1": {
        "title":     "{order} — First Ascension",
        "lore":      "{commander} has forged the {order}'s first true mark upon the realm. The order's ledger opens at a new level.",
        "impact":    "{order} advances to {rank}. Base XP granted to J_Claw.",
    },
    "rank_up_tier_2": {
        "title":     "{commander} Reaches Expertise",
        "lore":      "Through sustained effort and proven execution, {commander} and the {order} have reached the second ascension. Expertise is not given — it is earned.",
        "impact":    "{order} advances to {rank}. Base XP granted to J_Claw.",
    },
    "rank_up_tier_3": {
        "title":     "{commander} Approaches Mastery",
        "lore":      "The {order} stands at the threshold of mastery. {commander}'s command has brought the order to its third ascension — a rank few orders reach.",
        "impact":    "{order} advances to {rank}. Base XP granted to J_Claw.",
    },
    "rank_up_tier_4": {
        "title":     "{commander} Ascends to Legendary",
        "lore":      "The realm marks this day. {commander} and the {order} have achieved the highest tier — legendary status among all orders. This is a major event in the chronicles.",
        "impact":    "{order} reaches legendary rank: {rank}. Major base XP granted to J_Claw.",
    },
    # Streak milestones
    "streak_7": {
        "title":     "{order} — 7-Day Battle Rhythm",
        "lore":      "{commander}'s order has held an unbroken 7-day streak. Morale multiplier is now active.",
        "impact":    "XP multiplier ×1.1 applied to all {order} skill runs.",
    },
    "streak_14": {
        "title":     "{order} — 14-Day Iron Discipline",
        "lore":      "Fourteen days without a lapse. {commander}'s order has demonstrated true iron discipline.",
        "impact":    "XP multiplier ×1.2 applied to all {order} skill runs.",
    },
    "streak_21": {
        "title":     "{order} — 21-Day Veteran Rhythm",
        "lore":      "Three weeks of unbroken execution. The {order} is now operating at veteran battle tempo.",
        "impact":    "XP multiplier ×1.3 applied to all {order} skill runs.",
    },
    # Prestige
    "prestige": {
        "title":     "J_Claw Ascends — Prestige {prestige}",
        "lore":      "J_Claw has completed a full cycle of mastery and ascended to Prestige {prestige}. A permanent {multiplier}× multiplier now governs all future XP.",
        "impact":    "All future XP gains multiplied by ×{multiplier}.",
    },
    # Ruler reward
    "ruler_reward": {
        "title":     "Sovereign's Decree — {amount} XP",
        "lore":      "Matthew, the true sovereign, has decreed a reward for demonstrated excellence. {reason}",
        "impact":    "+{amount} base XP granted to J_Claw.",
    },
}


# ── Public helpers ─────────────────────────────────────────────────────────────

def get_division(key: str) -> dict:
    return DIVISIONS.get(key, {})

def get_all_skill_xp() -> dict:
    """Return flat dict: skill_name -> {division_key, xp, soldier, label}"""
    result = {}
    for div_key, div in DIVISIONS.items():
        for skill_key, skill in div.get("skills", {}).items():
            result[skill_key] = {
                "division": div_key,
                "xp":       skill["xp"],
                "soldier":  skill.get("soldier", skill_key),
                "label":    skill.get("label", skill_key),
                "icon":     skill.get("icon", "⚔"),
                "anim":     skill.get("anim", "slash"),
            }
    return result

def get_division_ranks(key: str) -> list:
    """Return list of {xp, title} dicts for a division."""
    div = DIVISIONS.get(key, {})
    ranks = div.get("ranks", [])
    from runtime.realm.config import DIV_XP_THRESHOLDS
    return [{"xp": DIV_XP_THRESHOLDS[i], "title": r} for i, r in enumerate(ranks)]

def rank_title_for_xp(division_key: str, xp: int) -> str:
    """Return the current rank title for a division given XP."""
    div = DIVISIONS.get(division_key, {})
    ranks = div.get("ranks", [])
    for i in range(len(DIV_XP_THRESHOLDS) - 1, -1, -1):
        if xp >= DIV_XP_THRESHOLDS[i] and i < len(ranks):
            return ranks[i]
    return ranks[0] if ranks else "—"

def tier_for_xp(xp: int) -> int:
    """Return 0-based tier index for given XP."""
    for i in range(len(DIV_XP_THRESHOLDS) - 1, -1, -1):
        if xp >= DIV_XP_THRESHOLDS[i]:
            return i
    return 0

def as_json() -> dict:
    """Return the full world config as a JSON-serializable dict for the API."""
    return {
        "jclaw":        JCLAW_IDENTITY,
        "divisions":    DIVISIONS,
        "base_ranks":   BASE_RANKS,
        "achievements": ACHIEVEMENTS,
        "xp_per_level": XP_PER_LEVEL,
        "thresholds":   DIV_XP_THRESHOLDS,
        "rank_up_base_xp": RANK_UP_BASE_XP,
    }
