# Comptage automatique des hypercharges depuis le jeu — design

Date : 2026-06-14
Statut : approuvé (mode autonome)

## Problème

L'estimateur de revente (`revente/`) a besoin du nombre d'hypercharges possédées
(`AccountData.hypercharges`) — c'est, avec les skins rares, le plus gros levier de
prix. Aujourd'hui ce champ est à 0 par défaut et jamais mesuré. brawlace ne donne
pas l'info et sa donnée est de toute façon périmée (liste P11 fausse : il annonçait
Béa P11 alors qu'elle est P1 in-game). On veut le lire automatiquement depuis le jeu.

## Faits établis (vérifiés en live sur le Mi9T, 2026-06-13/14)

- Device cible : Mi9T, écran physique 1080×2340 → BS en paysage **2340×1080 (19,5:9)**.
  Piloté via ADB depuis le worker HP (`192.168.60.18:5555`).
- **Détection HC sur la fiche détail = fiable** : l'hypercharge possédée s'affiche
  comme une **flamme magenta** (fleur blanche au centre, « 1/1 »). Sur le fond bleu
  uni de la colonne d'abilités : **1802 px magenta dans la zone HC vs 0 px** ailleurs.
- **Détection HC en grille = trop bruitée** : les fonds de cartes épiques (Maisie,
  Lola…) et des icônes UI sont magenta → impossible d'isoler la flamme par couleur
  seule dans la grille. Écarté.
- Collection : ~40 brawlers, tri par rareté, **grille 3 colonnes**, scroll par swipe.
  Bouton BRAWLERS ≈ (140, 470) px. Carte → tap ouvre la fiche détail ; flèche back
  haut-gauche revient. Chaque carte affiche un **badge de puissance** (cercle bas-gauche,
  ex. « 1 », « 4 », « 11 ») et le **nom** du brawler.
- P11 connus du compte au moment du design : Shelly, Brock, Bartaba, Maisie (Maisie a
  l'unique HC, achetée pour calibrer). brawlace : à ne jamais utiliser pour les P11.

## Approche retenue

**Détection sur fiche détail + navigation bornée aux cartes P11.** La fiche détail est
la seule source de détection prouvée fiable ; on ne visite que les P11 (≈5), pas les 40.

### Module `revente/read_hypercharges.py`

`count_hypercharges(serial) -> {"count": int, "brawlers": list[str]}`

1. **Ouvrir la collection** : tap BRAWLERS, screenshot, vérifier qu'on est sur la
   collection (en-tête « BRAWLERS (n/m) » détecté par OCR). Sinon retry borné.
2. **Scan page par page** : boucle de scroll (swipe), cap de sécurité `MAX_SCROLLS`.
   Sur chaque vue : pour chaque cellule de la grille (géométrie connue, jusqu'à 6
   visibles), OCR du badge de puissance. Repérer les cartes **P11** (et, par prudence
   anti-sous-comptage, les badges ambigus/≥9). Dédup par OCR du **nom** de carte
   (set des noms déjà traités). Fin quand un scroll ne révèle aucun nouveau brawler.
3. **Pour chaque carte P11 non encore traitée et visible** : tap (coords de la cellule)
   → screenshot → vérifier qu'on est sur une fiche détail (présence du panneau
   d'abilités à droite / « NIVEAU MAX ») → `_detail_has_hypercharge(img)` →
   enregistrer (nom lu sur la fiche) → tap back → vérifier retour grille. Retries +
   caps à chaque étape.
4. Retourner le compte et la liste des brawlers hypercharchés.

### Détecteur `_detail_has_hypercharge(pil_image, w, h) -> bool`

Crop la **colonne d'abilités droite** de la fiche détail (région large `x≈0.82–0.97`,
`y≈0.10–0.55`, fond bleu uni — robuste au décalage de layout selon le nb de
gadgets/star powers), compte les pixels magenta via une plage HSV calibrée, renvoie
`count >= HC_MIN_PX`. Justification du seuil : zone HC ≈ 1800 px magenta, fond bleu = 0.

### Géométrie résolution-aware

`_geom_for(w, h)` renvoie le jeu de coordonnées (bouton BRAWLERS, swipe scroll, rect
des 6 cellules, sous-région badge-puissance, sous-région nom, flèche back, région HC
fiche détail). Calibré pour **19,5:9 (Mi9T)** ; structuré pour ajouter le 16:9
BlueStacks plus tard (même motif que `read_currencies._crops_for`).

## Robustesse

- La **fiche détail fait foi**. Un faux positif P11 en grille = un tap gâché (détecteur
  renvoie False) — sans impact sur le compte. Le vrai risque est le faux *négatif* P11
  (sous-comptage) → on tape aussi les badges ambigus/≥9.
- Vérification d'état après chaque tap et chaque back (on est bien sur fiche / sur grille)
  avant de continuer ; retries bornés ; caps anti-boucle (`MAX_SCROLLS`, `MAX_TAPS`).
- Échec propre : si l'ouverture de collection ou la navigation échoue, retourner
  `{"count": None, "brawlers": []}` et laisser l'orchestrateur garder `hypercharges=0`
  (comportement actuel), jamais planter l'estimation.

## Intégration

- `estimate_account.run()` appelle `count_hypercharges(serial)` et remplit
  `AccountData.hypercharges` (+ liste des brawlers dans les notes). Les gemmes sont déjà
  intégrées (`read_currencies`, commit a093f7b).
- Modèle **opérateur** : l'estimation se lance bot en pause (comme la session de calibrage).
  Pas d'orchestration worker auto-pause dans ce périmètre.

## Hors périmètre (YAGNI)

- Détection auto des **skins rares** : aucune source fiable, et la *rareté* exige un œil
  humain → reste manuel.
- Commande worker `count_hypercharges` avec auto-pause/reprise : modèle opérateur suffit.
- Support 16:9 BlueStacks : structure prête mais non calibrée (cible = Mi9T).

## Tests

- **Unitaires sur fixtures réelles** (crops sauvegardés depuis les captures du Mi9T) :
  - `_detail_has_hypercharge` : Maisie (fiche avec HC) → True ; une fiche P11 **sans** HC
    (Shelly, à capturer) → False ; un crop de fond bleu → False.
  - parsing OCR badge puissance : carte Maisie → 11 ; carte P1 → 1.
  - `_geom_for(2340,1080)` renvoie le jeu « phone » attendu.
- **Validation live** sur le Mi9T en fin d'implémentation : `count_hypercharges` doit
  renvoyer `count=1, brawlers=["maisie"]` (état actuel du compte), bornée par les caps.
  Bot mis en pause pendant la validation, relancé après.
