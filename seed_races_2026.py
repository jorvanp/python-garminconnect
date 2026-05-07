"""
seed_races_2026.py — Carga competencias conocidas de 2026 en Firestore.

Uso:
    python seed_races_2026.py           # inserta las que faltan
    python seed_races_2026.py --dry-run # solo muestra lo que insertaría

Solo inserta si no existe ya una carrera con el mismo nombre Y fecha.
"""

import sys
import os
from dotenv import load_dotenv
load_dotenv()

RACES_2026 = [
    # ── World Marathon Majors ────────────────────────────────────────────────
    {"name": "Maratón de Tokio",             "race_type": "Maratón",       "event_date": "2026-03-01"},
    {"name": "Maratón de Boston",            "race_type": "Maratón",       "event_date": "2026-04-20"},
    {"name": "Maratón de Londres (TCS)",     "race_type": "Maratón",       "event_date": "2026-04-26"},
    {"name": "Maratón de Berlín (BMW)",      "race_type": "Maratón",       "event_date": "2026-09-27"},
    {"name": "Maratón de Chicago",           "race_type": "Maratón",       "event_date": "2026-10-11"},
    {"name": "Maratón de Nueva York (TCS)",  "race_type": "Maratón",       "event_date": "2026-11-01"},

    # ── Internacionales destacados ───────────────────────────────────────────
    {"name": "Maratón de Los Ángeles (ASICS)",      "race_type": "Maratón",       "event_date": "2026-03-08"},
    {"name": "Maratón de París",                    "race_type": "Maratón",       "event_date": "2026-04-12"},
    {"name": "Maratón de Valencia Trinidad Alfonso","race_type": "Maratón",       "event_date": "2026-12-06"},
    {"name": "Medio Maratón de Valencia",           "race_type": "Media maratón", "event_date": "2026-10-25"},

    # ── Rock 'n' Roll Running Series ────────────────────────────────────────
    {"name": "Rock 'n' Roll Arizona",         "race_type": "Media maratón", "event_date": "2026-01-18"},
    {"name": "Rock 'n' Roll Las Vegas",       "race_type": "Media maratón", "event_date": "2026-02-22"},
    {"name": "Rock 'n' Roll Nashville",       "race_type": "Maratón",       "event_date": "2026-04-26"},
    {"name": "Rock 'n' Roll Nashville",       "race_type": "Media maratón", "event_date": "2026-04-26"},
    {"name": "Rock 'n' Roll San Diego",       "race_type": "Maratón",       "event_date": "2026-05-31"},
    {"name": "Rock 'n' Roll San Diego",       "race_type": "Media maratón", "event_date": "2026-05-31"},
    {"name": "Rock 'n' Roll Seattle",         "race_type": "Maratón",       "event_date": "2026-06-18"},
    {"name": "Rock 'n' Roll Seattle",         "race_type": "Media maratón", "event_date": "2026-06-18"},
    {"name": "Rock 'n' Roll San Antonio",     "race_type": "Maratón",       "event_date": "2026-12-06"},
    {"name": "Rock 'n' Roll San Antonio",     "race_type": "Media maratón", "event_date": "2026-12-06"},

    # ── México ───────────────────────────────────────────────────────────────
    {"name": "Medio Maratón CDMX BBVA",               "race_type": "Media maratón", "event_date": "2026-07-12"},
    {"name": "Maratón de la Ciudad de México",         "race_type": "Maratón",       "event_date": "2026-08-30"},
    {"name": "Medio Maratón de Guadalajara Electrolit","race_type": "Media maratón", "event_date": "2026-02-22"},
    {"name": "Maratón de Guadalajara",                 "race_type": "Maratón",       "event_date": "2026-11-29"},
    {"name": "Maratón de Monterrey (Powerade)",        "race_type": "Maratón",       "event_date": "2026-12-13"},
    {"name": "Maratón de la Atenas Veracruzana",       "race_type": "Maratón",       "event_date": "2026-04-26"},
]

# Clave de deduplicación: (name_lower, event_date)
def _key(r):
    return (r["name"].strip().lower(), r["event_date"])


def main():
    dry_run = "--dry-run" in sys.argv

    import firestore_helper

    existing = firestore_helper.get_all_races()
    existing_keys = {_key(r) for r in existing}
    print(f"Carreras existentes en Firestore: {len(existing)}")

    to_insert = [r for r in RACES_2026 if _key(r) not in existing_keys]
    skipped   = len(RACES_2026) - len(to_insert)

    print(f"Ya registradas (se omiten): {skipped}")
    print(f"Por insertar: {len(to_insert)}")

    if not to_insert:
        print("Nada nuevo que insertar. ✓")
        return

    if dry_run:
        print("\n── DRY RUN — no se escribe nada ──")
        for r in to_insert:
            print(f"  [{r['event_date']}] {r['name']} ({r['race_type']})")
        return

    inserted = 0
    for r in to_insert:
        race_id = firestore_helper.create_race_admin(
            race_type=r["race_type"],
            event_date=r["event_date"],
            name=r["name"],
        )
        print(f"  ✓ {r['event_date']}  {r['name']}  ({r['race_type']})  → {race_id}")
        inserted += 1

    print(f"\nListo: {inserted} competencias insertadas.")


if __name__ == "__main__":
    main()
