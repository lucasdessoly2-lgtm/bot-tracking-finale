# =====================================================================
#  LISTE DES COMPTES INSTAGRAM À SURVEILLER
# ---------------------------------------------------------------------
#  Format 2 champs  : ("username_insta", "NOM_VA")
#  Format 3 champs  : ("username_insta", "NOM_VA", "shortcode_gms")
#
#  Le 3ème champ est OPTIONNEL et sert à matcher manuellement le lien GMS
#  quand le shortcode ne correspond PAS au username Instagram.
#  → Utile quand ton lien GMS est getmysocial.com/lauras et le compte
#    Instagram s'appelle Laura.sensoryx (pas de match automatique).
#
#  Règles :
#    - Garde TOUJOURS les parenthèses ( ), les guillemets " " et la virgule
#    - Le username Instagram = ce qui suit le @ sur le profil (sans le @)
#    - Le shortcode GMS = ce qui suit "/" dans l'URL GMS (sans le slash)
#    - Pour mettre un compte en pause sans le supprimer : ajoute un # devant
#      Exemple : # ("compte_en_pause", "VA_1"),
# =====================================================================

ACCOUNTS = [
    # ---------- STONE (1 comptes) ----------
    # ⚠️ Pour STONE, les shortcodes GMS sont DIFFÉRENTS du username Insta.
    # Remplace les 3 "TODO_..." par les vrais shortcodes GMS.
    ("Laura.vyxenna",  "STONE", "https:getmysocial.comlaura7"),

    # ---------- FANIEL (2 comptes) ----------
    ("itsyourivybb", "FANIEL"),
    ("notivyleee",   "FANIEL"),

    # ---------- COOP-MOOS (4 comptes) ----------
    ("babemmadi",   "COOP-MOOS"),
    ("ssbymadii",   "COOP-MOOS"),
    ("madiisonnno", "COOP-MOOS"),
    ("olivemadii",  "COOP-MOOS"),
]
