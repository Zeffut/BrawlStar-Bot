# Plan d'exécution — Revente comptes Phase 1 (Eldorado)

> **Plan opérationnel / business**, pas du code. Pas de TDD/tests. Tâches bite-sized avec livrable concret par étape.
> Légende : **[Toi]** = exige ton identité/accès/argent · **[Moi]** = je peux le produire (recherche, rédaction, structuration) · **[Ensemble]** = on le fait en binôme dans le chat.

**Goal :** Réaliser les 5 premières ventes propres de comptes haut-trophées sur Eldorado, avec 0 litige et payouts SEPA reçus, pour valider la demande avant de scaler.

**Approche :** S'appuyer sur l'escrow Eldorado pour amorcer sans réputation. Prix d'amorçage −10/−15 %. Suivi sur Google Sheets. SOP de handover stricte. Critère de sortie = ≥5 ventes propres → débloque la Phase 2.

**Spec source :** `docs/superpowers/specs/2026-05-31-revente-comptes-strategie-design.md`

---

## Tâche 1 — Préparer le compte vendeur Eldorado + KYC **[Toi]**

**Livrable :** un compte vendeur Eldorado opérationnel, KYC validé, méthode de payout SEPA configurée.

- [ ] **1.1** Créer un compte sur https://www.eldorado.gg avec un email dédié à l'activité de revente (pas ton perso principal)
- [ ] **1.2** Activer le profil vendeur (Sell / Become a seller)
- [ ] **1.3** Lancer le KYC : pièce d'identité + nom **exactement** identique à celui du compte bancaire de retrait, en alphabet latin
- [ ] **1.4** Configurer le payout **SEPA** (RIB FR). Vérifier que le nom du titulaire correspond au KYC
- [ ] **1.5** Noter les délais constatés (validation KYC, délai de payout post-confirmation) pour les reporter dans le suivi

> ⚠️ Sans KYC validé, pas de retrait possible. À faire en tout premier car ça peut prendre 24-48 h.

---

## Tâche 2 — Construire le tableur de suivi d'inventaire **[Moi → toi remplis]**

**Livrable :** un Google Sheets prêt à l'emploi avec les bonnes colonnes + une feuille « ventes » pour le suivi économique.

- [x] **2.1 [Moi]** ✅ Livré : `revente/inventaire_template.csv` + `revente/ventes_template.csv` (importables dans Google Sheets), seedés avec Zeffut5.0 & Zeffut2.0

**Feuille `Inventaire` :**
| ID | Email SCID | Accès (ref. gestionnaire mdp) | Device | Trophées actuels | Brawlers | Skins/légendaires notables | Hypercharges | Statut | Plateforme | Prix listé | Date listé |
|---|---|---|---|---|---|---|---|---|---|---|---|

Statut ∈ { grind, prêt, listé, vendu, livré }

**Feuille `Ventes` :**
| ID compte | Date vente | Prix brut | Commission (15%) | Frais payout | **Net reçu** | Acheteur (pseudo) | Date payout | Litige (O/N) | Notes |
|---|---|---|---|---|---|---|---|---|---|---|

- [ ] **2.2 [Toi]** Créer le Sheets, coller les 2 feuilles, stocker les identifiants sensibles dans ton gestionnaire de mdp (le tableur ne référence que l'ID)
- [ ] **2.3 [Toi]** Recenser tes comptes vieux disponibles → une ligne `Inventaire` par compte, statut réel (grind / prêt)

---

## Tâche 3 — Benchmark marché : fixer la fourchette de prix réelle **[Moi]**

**Livrable :** une fourchette de prix € par tranche de composition, basée sur les annonces Eldorado live au 2026-05-31.

- [x] **3.1 [Moi]** ✅ Relevé live fait (Eldorado + PlayerAuctions)
- [x] **3.2 [Moi]** ✅ Grille livrée : `revente/grille_prix.md` (palier × composition → fourchette + prix d'amorçage)
- [ ] **3.3 [Ensemble]** Caler le **prix d'amorçage** réel quand un compte atteint un palier vendable (rien de vendable aujourd'hui : stock à ~2,5-4k tr)

> Dépend de la Tâche 2.3 (connaître la composition réelle de tes comptes prêts). Si tu me donnes les compositions, je benchmarke immédiatement.

---

## Tâche 4 — Rédiger le template d'annonce + checklist captures **[Moi]**

**Livrable :** un modèle de titre + description réutilisable, et la liste exacte des captures à prendre.

- [x] **4.1 [Moi]** ✅ Template de titre livré dans `revente/annonce_template.md` (met en avant skins rares → hypercharges → Power 11 → trophées)
- [x] **4.2 [Moi]** ✅ Template de description livré (avec section livraison + mention escrow)
- [x] **4.3 [Moi]** ✅ Checklist captures livrée dans le même fichier

- [ ] **4.4 [Toi]** Prendre les captures de tes comptes prêts selon la checklist (masquer tout identifiant à l'écran)

---

## Tâche 5 — Mettre en vente les 1-2 premiers comptes **[Toi, avec mes textes]**

**Livrable :** 1-2 annonces actives sur Eldorado.

- [ ] **5.1 [Toi]** Créer l'annonce sur Eldorado, catégorie Brawl Stars accounts
- [ ] **5.2 [Toi]** Coller titre + description (Tâche 4), uploader les captures (Tâche 4.4)
- [ ] **5.3 [Toi]** Fixer le prix d'amorçage (Tâche 3.3)
- [ ] **5.4 [Toi]** Choisir l'**after-sale courte (5 j)** côté config annonce/livraison
- [ ] **5.5 [Toi]** Publier → passer le statut du compte à `listé` dans le tableur (Tâche 2)

---

## Tâche 6 — SOP handover à la première vente **[Toi, je supervise]**

**Livrable :** une première vente livrée proprement, payout déclenché, 0 litige.

> **Règle d'or : ne transmettre AUCUN identifiant tant que le paiement n'est pas gelé en escrow. Tout dans la messagerie Eldorado.**

- [ ] **6.1 [Toi]** À réception d'une commande : vérifier que le **paiement est bien en escrow** avant toute action
- [ ] **6.2 [Toi]** Retirer toute liaison tierce résiduelle du compte (Google Play / Apple Game Center / Facebook)
- [ ] **6.3 [Toi]** Transmettre l'email dédié + accès du Supercell ID via la messagerie Eldorado
- [ ] **6.4 [Toi]** Guider l'acheteur pour qu'il **bascule le Supercell ID vers SON propre email** (Settings → Supercell ID → changer email)
- [ ] **6.5 [Toi]** Si SMS Account Protection actif : transmettre les codes de récupération
- [ ] **6.6 [Toi]** Confirmer avec l'acheteur qu'il accède bien au compte
- [ ] **6.7 [Toi]** Faire valider/confirmer la commande côté acheteur sur Eldorado
- [ ] **6.8 [Toi]** **Couper tout accès résiduel** : ne plus jamais se reconnecter au compte ni à son email dédié
- [ ] **6.9 [Toi]** Archiver les captures/logs de la transaction (preuve anti-litige)
- [ ] **6.10 [Toi]** Mettre à jour les feuilles `Inventaire` (statut `livré`) et `Ventes` (montants nets) du tableur

### Interdits absolus (rappel)
- ❌ Jamais récupérer un compte vendu · ❌ Jamais d'accès résiduel · ❌ Jamais de finalisation hors plateforme

---

## Tâche 7 — Boucle d'amorçage : répéter jusqu'à 5 ventes **[Ensemble]**

**Livrable :** 5 ventes propres, payouts SEPA reçus, avis positifs accumulés.

- [ ] **7.1** Répéter Tâches 4→6 pour chaque compte prêt suivant
- [ ] **7.2 [Ensemble]** Après chaque vente : analyser la **vitesse de vente** (temps annonce→vente). Vend vite → remonter le prix vers le médian. Stagne → ajuster description/prix
- [ ] **7.3 [Toi]** Solliciter un avis acheteur après chaque livraison réussie
- [ ] **7.4 [Toi]** Provisionner le **buffer de remboursement** (mettre de côté une part des premiers nets pour couvrir un éventuel ban post-vente)

---

## Tâche 8 — Bilan & décision Phase 2 **[Ensemble]**

**Livrable :** un go/no-go documenté pour la Phase 2.

- [ ] **8.1 [Ensemble]** Vérifier le **critère de passage** : ≥5 ventes propres + payouts reçus + 0 litige + avis positifs
- [ ] **8.2 [Ensemble]** Calculer la marge nette réelle moyenne / compte (feuille `Ventes`) et la comparer au temps de grind
- [ ] **8.3 [Ensemble]** Si critère atteint → lancer le cycle spec→plan de la **Phase 2** (G2G en parallèle + Discord MM + vitrine social s'appuyant sur les avis Eldorado)
- [ ] **8.4 [Ensemble]** Si critère non atteint → diagnostiquer (prix ? composition ? présentation ?) et itérer la Phase 1

---

## Points laissés ouverts (à trancher en cours de route)
1. Palier de vente cible exact (15k / 20k / 25k) — à caler selon le temps de grind réel par palier (avec le dev)
2. Seuil fiscal de formalisation (micro-entreprise ?) — à surveiller dès volume régulier
3. Montant exact du buffer de remboursement (Tâche 7.4)
