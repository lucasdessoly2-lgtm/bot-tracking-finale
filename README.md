# Bot Tracking VA Instagram

Bot Telegram qui vérifie 2x/jour si les comptes Instagram des VA ont posté un Reel aux horaires prévus, et envoie un rapport groupé dans un canal Telegram.

## Fonctionnement

- **09h30 FR** : rapport matin (vérifie si chaque compte a posté autour de **07h30**)
- **20h00 FR** : rapport soir (vérifie si chaque compte a posté autour de **16h30**)
- Tolérance : ±30 min autour de l'horaire cible

Pour chaque compte, le rapport indique :
- ✅ Posté dans le créneau
- ⚠️ Posté hors créneau (le jour même)
- ❌ Pas de post du jour

Avec pour chaque post : heure, vues, likes, commentaires.

## Variables d'environnement (à configurer dans Railway)

| Nom | Description |
|---|---|
| `TELEGRAM_TOKEN` | Token du bot Telegram (BotFather) |
| `TELEGRAM_CHAT_ID` | ID du canal Telegram (commence par `-100...`) |
| `RAPIDAPI_KEY` | Clé d'API RapidAPI (Instagram Scraper 2025) |
| `RAPIDAPI_HOST` *(optionnel)* | Host RapidAPI si différent de `instagram-scraper-2025.p.rapidapi.com` |

## Modifier la liste des comptes

Éditer le fichier [`accounts.py`](./accounts.py) :

```python
ACCOUNTS = [
    ("username_insta", "NOM_DU_VA"),
    # ...
]
```

- Ajouter un compte : nouvelle ligne avec le format ci-dessus
- Mettre en pause sans supprimer : préfixer la ligne avec `#`
- Le bot redémarre automatiquement à chaque commit (Railway)

## Modifier les horaires de vérification

Dans [`bot.py`](./bot.py), section **JOBS PROGRAMMÉS** :

```python
scheduler.add_job(job_matin, "cron", hour=9, minute=30)
scheduler.add_job(job_soir,  "cron", hour=20, minute=0)
```

Modifier `hour=` et `minute=` puis commit. Railway redéploie automatiquement.

## Déploiement

Voir le SOP joint pour les 5 phases : Telegram → Canal → RapidAPI → GitHub → Railway.

## Coût

~3 $/mois (uniquement le plan RapidAPI BASIC). Le reste est gratuit.
