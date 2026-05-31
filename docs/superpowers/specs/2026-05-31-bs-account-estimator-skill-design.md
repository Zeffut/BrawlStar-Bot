# Skill « bs-account-estimator » — Design

**Date :** 2026-05-31
**Type :** Skill Claude Code (contrôle émulateur + extraction + valorisation de comptes Brawl Stars)
**Statut :** En attente de review

> Objectif : un skill que Claude invoque pour, en autonomie, piloter un émulateur BlueStacks Air sur le Mac, se connecter à un compte Brawl Stars, extraire ses données lisibles, et produire une estimation de prix de revente.

---

## 1. Décisions actées

| Dimension | Décision |
|---|---|
| **Émulateur** | **BlueStacks Air** (natif Apple Silicon), installé sur le Mac, piloté via ADB (`adb connect 127.0.0.1:<port>`) |
| **Connexion comptes** | **Login auto Supercell ID** : le skill logout/login entre comptes ; codes de vérif lus via **IMAP** |
| **Profondeur de lecture** | **Base robuste** (trophées, brawlers, Power 11, gemmes, or, niveau) + **captures collection** best-effort. Skins/hypercharges = lecture visuelle des captures, pas d'énumération brittle |
| **Déclenchement** | À la demande (pas de récurrence programmée pour l'instant) |
| **Risque assumé** | Login multi-comptes depuis un émulateur/une IP = risque de ban Supercell sur le stock. Assumé par Zeffut. |

---

## 2. Architecture — réutilise 80 % du repo existant

### Réutilisé tel quel (machinerie déjà en place)
| Brique | Source |
|---|---|
| Résolution ADB + détection émulateur (`127.0.0.1:*`) | `device.py:adb_serial()`, `game_api._is_emulator_serial()` |
| Capture écran (H264 + fallback screencap) | `screen_capture.py:ScreenRecorder`, `game_api._adb_screencap()` |
| Input tap/swipe/keyevent | `game_api.tap()`, `_long_press()`, `_tap_back()` |
| Détection d'état (lobby/popup/shop…) | `state_finder/main.py:get_state()` (templates + OCR) |
| OCR trophées | `game_api.read_trophies()` |
| Détection tag joueur | `account_detect.detect_player_tag()` |
| Liste brawlers + power | `game_api.list_brawlers()` / brawlace |
| Navigation lobby | `game_api.ensure_brawlstars_at_lobby()`, `goto_lobby()` |

### À créer (n'existe pas)
| Module | Rôle |
|---|---|
| **`revente/login_supercell.py`** | Naviguer Réglages → Supercell ID → logout → login → saisir email → récupérer le code via IMAP → saisir le code → vérifier connexion |
| **`revente/imap_codes.py`** | Se connecter à la boîte mail (IMAP), poller le dernier code de vérif Supercell (6 chiffres), avec timeout |
| **`revente/read_currencies.py`** | OCR gemmes / or / niveau XP depuis l'écran lobby (crops dédiés — les valeurs sont en haut de l'écran) |
| **`revente/estimate.py`** | Étage valorisation : agrège les données → applique `revente/grille_prix.md` → $ estimé + niveau de confiance → écrit dans `revente/inventaire_template.csv` |
| **`revente/capture_collection.py`** | Best-effort : naviguer à la collection brawlers, capturer 1-N écrans pour lecture visuelle (skins/hypercharges) |

### Le skill (packaging)
- Emplacement : **`.claude/skills/bs-account-estimator/SKILL.md`** (project-level, versionné avec le repo)
- `SKILL.md` orchestre les scripts ci-dessus et documente l'usage
- Invocation : Claude lance le skill avec un compte cible (email Supercell ID ou « compte actuellement chargé ») → reçoit un rapport d'estimation

---

## 3. Flux d'estimation d'un compte (bout-en-bout)

```
1. S'assurer que BlueStacks Air tourne + adb connecté (127.0.0.1:<port>)
2. ensure_brawlstars_at_lobby()
3. [si compte cible ≠ compte chargé] → login_supercell(email) :
   a. nav Réglages → Supercell ID → Déconnexion
   b. Connexion → saisir email
   c. imap_codes.wait_for_code(email) → 6 chiffres
   d. saisir le code → confirmer
   e. ensure lobby + vérifier le tag détecté == attendu
4. Lire : read_trophies(), detect_player_tag(), list_brawlers() (→ nb brawlers, Power 11),
          read_currencies() (gemmes, or, niveau)
5. capture_collection() (best-effort) → screenshots pour lecture skins/hypercharges
6. estimate() → applique grille_prix.md → { palier, compo, prix_estimé, confiance, leviers_manquants }
7. Écrire la ligne dans inventaire_template.csv + retourner le rapport
```

---

## 4. Données extraites & valorisation

| Donnée | Source | Fiabilité |
|---|---|---|
| Trophées totaux | OCR lobby + somme brawlers (brawlace) | 🟢 |
| Nb brawlers / nb Power 11 | brawlace | 🟢 |
| Gemmes / Or / Niveau XP | OCR lobby (crops dédiés) | 🟡 (à calibrer sur BlueStacks 1920×1080) |
| Skins / Hypercharges | Captures collection → lecture visuelle | 🔴 best-effort |

Sortie de `estimate()` : un objet `{ tag, name, trophées, brawlers, power11, gemmes, or, niveau, prix_estimé_min, prix_estimé_max, confiance, note }`. `note` liste explicitement les leviers non mesurés (skins/hypercharges) → confiance abaissée tant qu'ils ne sont pas confirmés visuellement.

---

## 5. Setup one-time requis de Zeffut (Phase 0)

Incompressible (GUI / credentials / 2FA — je ne peux pas le faire à ta place) :
1. **Installer BlueStacks Air** (.dmg) sur le Mac
2. **Se connecter à un compte Google** dans BlueStacks (pour le Play Store)
3. **Installer Brawl Stars** depuis le Play Store
4. **Se logger sur un premier compte** Brawl Stars (valide le flux + me donne un compte à lire)
5. **Me communiquer le port ADB** de BlueStacks (Réglages → Avancé → Android Debug Bridge) — je teste `adb connect`
6. **Phase 2 uniquement** : accès **IMAP** à la boîte mail des Supercell ID (host, port, user, mot de passe d'application) + la **liste des emails** des comptes à estimer

---

## 6. Risques

| Risque | Note |
|---|---|
| **Ban Supercell (multi-login + émulateur)** | Assumé. Mitigation : espacer les logins, ne pas enchaîner trop vite, idéalement 1 compte de test d'abord. |
| **BlueStacks détecté par BS** | Possible refus de lancement / flag. À valider dès Phase 0 (BS se lance-t-il proprement ?). |
| **Fragilité OCR/nav** | Le login Supercell ID est de la nav UI fragile + saisie texte ADB. Phase la plus risquée (Phase 2). |
| **Sécurité IMAP** | Le mot de passe d'application IMAP transitera dans la session + sera lu par `imap_codes.py`. Stocker hors-repo (gitignore), jamais committer. Utiliser un **mot de passe d'application** dédié, révocable. |
| **Coordonnées hardcodées** | Les crops/regions sont calibrés 1920×1080 ; BlueStacks Air doit être réglé sur cette résolution, sinon recalibrer. |

---

## 7. Plan par phases

- **Phase 0 — Setup émulateur** *(Zeffut : §5.1-5)* : BlueStacks Air + Google + BS + 1 compte loggé + port ADB. **Bloquant pour la suite.**
- **Phase 1 — Lecture + valorisation** *(moi, autonome)* : adb connect, réutiliser device-control, `read_currencies.py`, `estimate.py`, scaffolding du skill. Testable dès qu'un compte est chargé.
- **Phase 2 — Login auto** *(moi + creds IMAP de Zeffut)* : `login_supercell.py` + `imap_codes.py`. Tester d'abord sur 1 compte de faible valeur.
- **Phase 3 — Capture collection** *(moi, best-effort)* : `capture_collection.py` + lecture visuelle skins/hypercharges.

---

## 8. Hors scope
- Énumération auto fiable des skins/hypercharges (nav trop fragile — best-effort visuel seulement)
- Récurrence programmée (à la demande pour l'instant)
- Multi-instance BlueStacks (on reste sur 1 instance + login auto, par choix)
- Intégration au panel cloud / au bot de grind (le skill est un outil d'estimation séparé)

---

## 9. Questions ouvertes
1. Résolution BlueStacks Air à fixer (1920×1080 recommandé pour coller aux crops existants)
2. Faut-il un compte « jetable » de test pour valider le login auto avant de toucher aux comptes à vendre ? (recommandé)
3. Où stocker les creds IMAP (proposé : `cfg/imap.toml`, gitignored)
