# Stratégie de revente des comptes Brawl Stars — Design

**Date :** 2026-05-31
**Auteur :** Zeffut + Claude
**Statut :** En attente de review
**Type :** Stratégie / opérations (aucun code dans cette phase)

> Ce document couvre **le volet business** de BrawlStar-Bot : écouler les comptes grindés par le bot. Le volet dev (bot, vision, automation) est traité séparément avec l'autre instance.

---

## 1. Décisions actées (synthèse du brainstorming)

| Dimension | Décision |
|---|---|
| **Produit** | Comptes **haut-trophées 15 000–30 000+** (palier de vente principal), avec brawlers + progression naturellement accumulés pendant le grind long |
| **Marché cible** | FR/EU |
| **Paiement** | PayPal / virement → **payout SEPA** |
| **Plateforme Phase 1** | **Eldorado.gg** (séquestre/escrow) |
| **Phase 2 (optionnelle)** | Discord avec middleman (MM) + G2G, **uniquement si la Phase 1 valide la demande** |
| **Volume** | Démarrer petit, scaler progressivement si ça vend |
| **Inventaire** | Comptes **déjà vieux** (faible risque de ban Supercell), **email dédié par compte** que Zeffut contrôle |

---

## 2. Le constat stratégique fondateur

Quand on part de **zéro réputation**, le problème n°1 n'est pas « où sont les acheteurs » mais **« pourquoi un acheteur ferait confiance à un vendeur inconnu de comptes de jeu »**. Le marché FR des comptes BS est saturé de scams. Un compte vendeur neuf à 0 vente est invendable en direct (Insta/Snap), quel que soit le prix.

**Conséquence :** on s'appuie d'abord sur l'**infrastructure de confiance des autres** (escrow + système d'avis d'une marketplace), on accumule des preuves (ventes propres, avis, zéro litige), puis seulement on rapatrie la vente chez nous (Phase 2) à marge plus élevée.

**Le pivot produit (mi-gamme → haut-trophées) :** la tranche 3 000–8 000 trophées se vend **3–20 $ brut**, marge nette quasi nulle après commission + frais + temps de grind. La valeur réelle commence vers **15 000–30 000+ trophées (20–80 $+)**. Le bot a un avantage décisif sur ce segment : il grind **longtemps et sans fatigue** des paliers que les humains détestent atteindre.

---

## 3. Plateforme — pourquoi Eldorado en Phase 1

| Plateforme | Commission | Payout EU | Hold / cashflow | Verdict Phase 1 |
|---|---|---|---|---|
| **Eldorado.gg** | 15 % flat (BS) | **SEPA**, Skrill, crypto (plus de SWIFT depuis 05/2026) | **Payout garanti dès confirmation acheteur** | ✅ **Retenu** |
| G2G | 12,99 % débutant → 8 % au rang | SEPA/SWIFT, PayPal, Payoneer, Skrill, crypto | ❌ **Hold 14 j sur les comptes** | Phase 2 (commission plus basse en scalant) |
| PlayerAuctions | ~10 % (non publié clairement) | PayPal, Skrill, Payoneer, virement | Payout 3-7 j + after-sale | Option de diversification |

**Raisons du choix Eldorado :**
- **~6 600 annonces BS actives** → trafic acheteur réel, marché liquide
- **Payout SEPA garanti dès que l'acheteur confirme la livraison**, indépendamment de ce qui arrive ensuite côté acheteur → cashflow rapide et sécurisé pour un nouveau vendeur
- Pas de hold de 14 jours comme G2G (qui plombe le cashflow au démarrage)

**Coût du choix :** commission de 15 % (la plus haute) + plus de SWIFT (mais SEPA suffit pour la France). On accepte la commission contre la **rapidité + sécurité du cash** au démarrage. Quand le volume justifiera de baisser les frais, on ouvrira **G2G en parallèle** (commission descend à 8 % avec le rang).

**Prérequis :** **KYC obligatoire** (vérification d'identité). Compte de retrait au nom exact de Zeffut, alphabet latin. À préparer avant la première vente.

---

## 4. Stratégie de pricing

### Principe : la réputation vaut plus que la marge sur les 5 premières ventes
Sur les **premières ventes**, on se positionne **−10 à −15 % sous le marché** pour des comptes de composition équivalente. Objectif : décrocher vite les premières transactions + avis, sortir du « 0 vente ». Une fois noté (≥5 avis positifs), on remonte au prix marché, voire légèrement au-dessus si la présentation est soignée.

### Méthode de fixation du prix (à chaque mise en vente)
Le marché price sur la **composition globale**, PAS sur les trophées seuls. Workflow :
1. Filtrer sur Eldorado les annonces de **composition comparable** (trophées + nombre de brawlers + skins/légendaires ± équivalents)
2. Relever la fourchette des 5-10 annonces les plus proches
3. Se positionner en bas de fourchette au lancement (amorçage), au milieu une fois noté

### Facteurs de premium (du plus impactant au moins)
1. **Skins rares / exclusifs** (ex. Star Shelly 2018, skins early Brawl Pass) — premium collector
2. **Brawlers légendaires / mythiques niveau max (Power 11)**
3. **Hypercharges** (chaque hypercharge manquante = ~5 000 gold à combler ; un brawler comme Sirius ≈ +20 % de prix)
4. Nombre total de brawlers débloqués
5. Gemmes, prestige (point de sauvegarde permanent à 1 000 / 3 000 tr)
6. **Trophées bruts** (impact le plus faible isolément, mais signal de « gros compte »)

### Repère économique (à affiner avec les comps réelles au moment de lister)
- Compte ~20 000 tr bien fourni : **~30–50 $ brut** côté haut de la fourchette
- −15 % commission Eldorado − frais SEPA → **net ~25–40 $ / compte**
- vs ~5 $ net en mi-gamme → le pivot **multiplie la marge unitaire par ~6-8**
- Coût marginal de prod ≈ électricité + usure device (le grind tourne déjà)

> ⚠️ Les chiffres de prix sont des fourchettes de listings/guides, pas une cote officielle. **Toujours benchmarker les annonces live au moment de lister.**

---

## 5. Process de livraison (SOP handover)

Atout majeur : **chaque compte a déjà un email dédié contrôlé par Zeffut** → handover propre et simple.

### Règle d'or
**Ne livrer AUCUN identifiant tant que le paiement n'est pas gelé en escrow.** Tout se passe **dans la plateforme** (jamais Discord/Reddit/DM en parallèle) — sinon perte de la protection vendeur.

### Étapes (une fois le paiement en escrow)
1. **Vérifier l'état du compte** : retirer toute liaison tierce résiduelle (Google Play, Apple Game Center, Facebook) avant transfert
2. **Transmettre l'email dédié + accès** du Supercell ID à l'acheteur via la messagerie de la plateforme
3. **L'acheteur bascule le Supercell ID vers son propre email** (Settings → Supercell ID → changer email) — idéalement on guide l'acheteur pour qu'il le fasse lui-même = preuve qu'il a bien le contrôle
4. Si **SMS Account Protection** activé : transmettre les **codes de récupération de secours**
5. **Confirmer que l'acheteur accède bien** avant de finaliser
6. **Couper tout accès résiduel** côté Zeffut : ne plus jamais se reconnecter au compte ni à l'email
7. **Documenter** : captures + logs de la transaction conservés comme preuve anti-litige

### Interdits absolus
- ❌ Ne **jamais** récupérer le compte après vente (= perte du payout + ban du compte vendeur sur la plateforme)
- ❌ Ne **jamais** garder un accès email/Google/Apple résiduel
- ❌ Ne **jamais** finaliser une vente hors plateforme

---

## 6. Gestion des risques

| Risque | Couverture / mitigation |
|---|---|
| **Chargeback PayPal** | Couvert : Eldorado garantit le payout dès confirmation acheteur |
| **Arnaque acheteur** (prétend ne pas avoir reçu) | Couvert par l'escrow + logs/captures de livraison ; faire basculer l'email par l'acheteur = preuve d'accès |
| **Récupération du compte par l'ancien proprio** | Mitigé par la SOP : zéro accès résiduel ; couvert par la fenêtre after-sale |
| **Ban Supercell post-vente** | **Non couvert au-delà de l'after-sale.** Mitigation principale : **comptes vieux** (faible flag) + after-sale **courte (5 j Eldorado)** + petit **buffer de remboursement** mis de côté |
| **KYC / identité** | Préparer pièce d'identité + compte de retrait au nom exact avant la 1re vente |
| **Fiscal (FR)** | Les revenus de revente sont **imposables / à déclarer**. À surveiller dès que le volume devient régulier. |
| **CGU Supercell** | La vente de comptes **viole les CGU Supercell** (grey market). Risque assumé et structurel ; aucune plateforme ne protège contre ça. |

---

## 7. Suivi de l'inventaire (Phase 1 = tableur)

En Phase 1, **pas de code** : un simple **Google Sheets** suffit. Migration possible vers le panel cloud existant plus tard (à coordonner avec le dev).

Colonnes minimales :

| Champ | Exemple |
|---|---|
| ID interne | BS-001 |
| Email Supercell ID | (privé) |
| Mot de passe / accès | (privé) |
| Device de grind | Mi9T / HP / PC UPEC |
| Palier trophées actuel | 18 400 |
| Brawlers / skins notables | 62 brawlers, 3 légendaires max, 12 skins |
| Statut | grind / prêt / listé / vendu / livré |
| Plateforme | Eldorado |
| Prix listé | 39 $ |
| Prix vendu net | 31 $ |
| Date vente | — |
| Acheteur (pseudo plateforme) | — |
| Notes / litige | — |

> Les identifiants sensibles peuvent être stockés à part (gestionnaire de mots de passe) et seulement référencés dans le tableur.

---

## 8. Workflow opérationnel bout-en-bout

```
Grind (bot) → compte atteint le palier cible (15k-30k+)
   → Snapshot : captures écran (profil, brawlers, skins, trophées)
   → Création annonce Eldorado (titre + description + captures + prix d'amorçage)
   → Vente (paiement gelé en escrow)
   → Handover SOP (section 5)
   → Confirmation acheteur → payout SEPA garanti
   → MAJ tableur + archivage des preuves
```

---

## 9. Plan de lancement (2 premières semaines)

**Semaine 1 — Mise en place**
1. Créer le compte vendeur Eldorado + passer le **KYC**
2. Vérifier l'état de liaison des comptes prêts (email dédié OK, liaisons tierces retirées)
3. **Benchmarker 10 annonces comparables** sur Eldorado (composition équivalente) → fourchette de prix
4. Préparer **1-2 comptes** au palier cible
5. Rédiger un **template d'annonce** (titre + description + checklist de captures)
6. Lister à **prix d'amorçage (−10/−15 %)**

**Semaine 2 — Première traction**
7. Première(s) vente(s) → exécuter la **SOP handover** rigoureusement
8. Récolter les **premiers avis**
9. Ajuster le prix selon la vitesse de vente

**Critère de passage à la Phase 2 (Discord MM / G2G / vitrine social) :**
> ✅ **≥ 5 ventes propres**, payouts SEPA reçus, **0 litige**, avis positifs accumulés.

---

## 10. Phase 2 (optionnelle, si Phase 1 validée)

- **G2G en parallèle** : profiter de la commission qui descend à 8 % avec le rang (diversification + meilleure marge)
- **Discord FR avec middleman (MM)** : vente à frais ~0, en s'appuyant sur les **avis Eldorado comme preuve de confiance**
- **Vitrine réseaux sociaux** (Insta/Snap/TikTok) : vente directe à marge max, légitimée par « +X ventes vérifiées, avis Eldorado »

Chaque canal de Phase 2 fera l'objet de son propre cycle spec → plan si on y va.

---

## 11. Hors scope (Phase 1)

- ❌ Outillage / automatisation de la mise en vente (tableur manuel suffit pour valider la demande)
- ❌ Intégration au panel cloud
- ❌ Optimisation de la composition du bot (skins/légendaires) — le dev pur s'en occupe ; ici on vend ce que le grind produit
- ❌ Canaux Phase 2 (traités plus tard)

---

## 12. Questions ouvertes / à valider à la review

1. Le palier de vente cible exact (15k ? 20k ? 25k ?) sera affiné selon le temps de grind réel par palier (à mesurer avec le dev).
2. Décision fiscale : seuil de volume à partir duquel on formalise (micro-entreprise ?).
3. Montant du buffer de remboursement à provisionner.
