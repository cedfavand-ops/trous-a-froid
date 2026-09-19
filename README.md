# Trous à froid — Prévisions multi-stations

Page unique avec onglets, regroupant plusieurs stations (Gréolières-les-Neiges,
Doline de Chaud Clapier, La Pesse - Le Cernétrou, et d'autres à venir), chacune
avec sa propre correction nocturne du trou à froid apprise indépendamment à
partir de sa station Datacake.

## Ce qui a changé par rapport aux dépôts séparés

- **Un seul workflow** génère les prévisions de toutes les stations en un
  passage horaire (`scripts/update_forecast.py` boucle sur la liste `STATIONS`).
- **Un fichier JSON par station** : `data/<slug>.json` (prévisions) et
  `data/<slug>_bias_state.json` (profil de correction appris, indépendant
  d'une station à l'autre).
- **`index.html` unique** avec des onglets pour switcher entre stations, sans
  recharger la page.
- La logique de correction elle-même (nébulosité effective, profil non-linéaire
  par case horaire, apprentissage par moyenne mobile, lissage entre cases
  voisines) est **strictement identique** à ce qui tournait sur les dépôts
  séparés — seule l'architecture change.

## ⚠️ Important : récupérer l'apprentissage déjà accumulé

Si tu migres depuis les dépôts séparés `greolieres-meteo` et
`chaud-clapier-meteo` qui tournaient déjà depuis plusieurs jours, **ne repars
pas de zéro** : récupère le contenu de leurs `data/bias_state.json` respectifs
et colle-le dans :

- `data/greolieres_bias_state.json` (à la place du contenu vide fourni)
- `data/chaud-clapier_bias_state.json` (idem)

Sinon tout l'apprentissage déjà fait (plusieurs nuits de profil appris) sera
perdu et il faudra recommencer à converger depuis les valeurs par défaut.

## Mise en route

1. Crée un nouveau dépôt GitHub, pousse-y tout le contenu de ce dossier.
2. **Récupère l'apprentissage existant** (voir ci-dessus) avant le premier run.
3. Active **GitHub Pages** (Settings → Pages → branche `main`, dossier `/root`).
4. Dépose tes pictogrammes dans `icons/` (partagés par toutes les stations).
5. Pour **chaque station**, crée les 3 secrets GitHub Actions
   (Settings → Secrets and variables → Actions) au format
   `DATACAKE_TOKEN_<SUFFIX>`, `DATACAKE_DEVICE_ID_<SUFFIX>`,
   `DATACAKE_TEMP_FIELD_<SUFFIX>` — le suffixe est celui indiqué dans
   `STATIONS` (`scripts/update_forecast.py`) :
   - `GREOLIERES`
   - `CHAUD_CLAPIER`
   - `LA_PESSE`
   - `TIGNES`
   - `BEUIL`

   Chaque station peut avoir un token issu d'un compte Datacake différent —
   aucun problème, ce sont des secrets indépendants.
6. Lance le workflow manuellement une première fois (Actions → Run workflow).

## Ajouter une nouvelle station

Trois endroits à modifier :

1. **`scripts/update_forecast.py`** : ajouter une entrée à `STATIONS` (slug,
   nom, lat/lon, altitude, lien Datacake, suffixe d'env).
2. **`index.html`** : ajouter la même station au tableau JS `STATIONS` en haut
   du `<script>` (slug, nom, lien Datacake).
3. **`.github/workflows/update.yml`** : ajouter les 3 lignes `env:`
   correspondantes, puis créer les 3 secrets GitHub associés.

Pas besoin de créer manuellement `data/<slug>.json` à l'avance : le premier
passage du workflow le génère automatiquement (la page affiche un message
"prévisions pas encore disponibles" pour cet onglet en attendant).

## Réglages ajustables

Toujours en haut de `scripts/update_forecast.py`, communs à toutes les
stations pour l'instant (`CLEAR_CLOUD_THRESHOLD`, `CALM_WIND_THRESHOLD`,
`HIGH_CLOUD_ATTENUATION`, `DEFAULT_ALPHA`, etc.) — voir les commentaires dans
le fichier pour le détail de chacun.
