# Guide d'installation — PrimeNF Hub (3 magasins Netfact2)

Ce guide explique comment installer **tout le système**, de zéro, dans l'ordre.
Gardez-le sous la main : chaque section correspond à un poste précis.

> **Rappel matériel**
> - **Magasin 1** = le *hub* (toujours allumé). Il garde Python, la base centrale `central.db`, le serveur web, et c'est lui qui envoie les notifications.
> - **Magasins 2 & 3** = postes qui s'éteignent le soir. Ils ne reçoivent **qu'un seul fichier `.exe`** (aucun Python à installer).
> - **TVA** : le système est configuré pour **TTC = HT** (pas de TVA appliquée sur le prix de vente). C'est volontaire.

---

## Vue d'ensemble en 5 étapes

| Étape | Où | Quoi |
|------|------|------|
| 1 | Les 3 postes | Installer **Tailscale** (réseau privé) |
| 2 | Magasin 1 | Installer Python + dépendances, créer `stores.json`, charger `central.db`, lancer le hub |
| 3 | Magasin 1 (ou PC dev) | Construire les `.exe` avec PyInstaller |
| 4 | Magasins 2 & 3 | Copier `PrimeNFAgent.exe` + `stores.json`, lancer `install_agent.bat` |
| 5 | N'importe quel poste | Lancer l'application bureau `PrimeNFHub.exe` |

---

## Ce qui est DÉJÀ intégré dans la branche

Vous n'avez **rien à coder**. La branche `claude/multi-store-db-access-bned12` contient déjà :

- ✅ **Import Bon de Réception** (Excel **et PDF**) — intégré dans la page « Import BDR » de l'app bureau, et appliqué automatiquement aux magasins hors-ligne via la file d'attente.
- ✅ **Éditeur de prix en masse** — intégré dans la page « Éditeur de prix », applicable à **1 ou aux 3 magasins** (produits identiques = même prix). TTC = HT.
- ✅ **Trésorerie** (encaissements du jour par mode de paiement).
- ✅ **Synchronisation** + tableau de bord web + notifications Telegram/ntfy.

Les outils d'origine (`import_bon_reception.py`, `article_db.py`, `editor_logic.py`) sont réutilisés **tels quels** dans `src/vendor/` — aucune logique métier n'a été réécrite.

> ⚠️ **Important — à tester sur vos vraies bases.**
> Le code est complet et syntaxiquement validé, mais il n'a **pas encore tourné contre une vraie base `DIFA2.FDB`**. Les requêtes de lecture (stock, trésorerie) s'adaptent au schéma à l'exécution, mais lancez **`verify.py`** sur chaque poste d'abord, et faites un test BDR à 2 lignes avant un vrai import. Voir la section « Vérification » en bas.

---

## Étape 1 — Tailscale (les 3 postes)

Tailscale crée un réseau privé : chaque PC obtient une adresse fixe `100.x.y.z`, même derrière des box différentes, sans ouvrir de ports.

1. Créez un compte gratuit sur https://tailscale.com (un seul compte pour les 3).
2. Sur **chaque** PC : téléchargez et installez Tailscale, connectez-vous avec ce compte.
3. Dans l'admin Tailscale (https://login.tailscale.com/admin/machines), notez l'adresse `100.x.y.z` de **chaque** poste.
4. Vérifiez que le magasin 1 voit les autres : depuis le magasin 1, ouvrez `cmd` et tapez
   `ping 100.x.y.z` (l'adresse du magasin 2). Ça doit répondre quand le magasin 2 est allumé.

> Le serveur Firebird de Netfact2 écoute déjà sur le port **3050**. Tailscale rend ce port accessible depuis le hub sans rien configurer d'autre.

---

## Étape 2 — Magasin 1 (le hub)

### 2.1 — Installer Python

1. Téléchargez **Python 3.11+** sur https://www.python.org/downloads/windows/
2. À l'installation, **cochez « Add Python to PATH »**.

### 2.2 — Récupérer le projet

```bat
git clone -b claude/multi-store-db-access-bned12 <URL_DU_DEPOT> bulk-nf2
cd bulk-nf2
```

### 2.3 — Installer les dépendances du hub

```bat
python -m pip install -r requirements_hub.txt
```

> Utilisez toujours `python -m pip install ...` (et non `pip install ...` tout court).
> Si plusieurs Python sont installés sur le PC, `pip` tout seul peut installer
> dans le mauvais Python — `python -m pip` installe garanti dans celui que
> `python` utilisera pour lancer les scripts.
>
> **Si la commande `python` seule ne fonctionne pas** (ou installe/exécute dans
> le mauvais Python), utilisez le **lanceur Windows `py`** à la place, en
> remplaçant `python` par `py -3.11` **partout** dans ce guide (adaptez `3.11`
> à votre version si différente — `py -0` liste les versions installées) :
> ```bat
> py -3.11 -m pip install -r requirements_hub.txt
> py -3.11 backfill_store1.py --stores stores.json --db central.db
> py -3.11 hub_server.py --db central.db --port 5000
> ```
> C'est **la même règle** pour toutes les commandes `python ...` et
> `pip install ...` du reste de ce document.

### 2.4 — Créer et remplir `stores.json`

```bat
copy stores.json.example stores.json
notepad stores.json
```

Remplissez :
- **`host`** de **chaque** magasin, y compris le magasin 1 (hub) = son adresse
  Tailscale `100.x.y.z` (visible dans https://login.tailscale.com/admin/machines).
  Ne mettez **jamais** `localhost` pour le magasin 1 : le même `stores.json`
  est copié tel quel sur les 3 postes, et les magasins 2/3 ont besoin de la
  vraie adresse réseau du hub pour le joindre — `localhost` ne voudrait dire
  quelque chose que pour le poste du hub lui-même.
- **`database`** = chemin de la base sur **ce** poste (ex. `C:\\Netfact\\Data\\DIFA2.FDB`).
- **`password`** = mot de passe SYSDBA (souvent `masterkey`).
- **`hub_api_key`** = une longue chaîne aléatoire que vous inventez (utilisée par les agents).
- **`access_code`** = un **code court et mémorisable** (ex. `DIFA-2026`) que vous taperez pour connecter un appareil quelconque (PC perso, etc.) sans copier `stores.json`. Changez-le quand vous voulez révoquer l'accès d'un appareil.
- **`telegram.bot_token`** et **`telegram.chat_id`** (voir étape 2.7), ou **`ntfy_topic`**.

> `stores.json` n'est **jamais** envoyé sur git (il contient vos mots de passe). C'est `stores.json.example` qui sert de modèle.

### 2.5 — Charger la base centrale (une fois)

```bat
python backfill_store1.py --stores stores.json --db central.db
```

Vous devez voir le nombre de lignes chargées par table (article, piece, stock_snapshot…).

### 2.6 — Lancer le serveur hub

Pour un essai :
```bat
python hub_server.py --db central.db --port 5000
```
Ouvrez ensuite `http://localhost:5000` dans le navigateur → le tableau de bord apparaît.

Pour qu'il tourne **en permanence** comme service Windows (recommandé) :
1. Téléchargez `nssm.exe` sur https://nssm.cc et placez-le dans `install_windows\`.
2. Lancez (en Administrateur) :
   ```bat
   install_windows\install_hub.bat
   ```
   Le service `PrimeNFHub` démarre désormais automatiquement au boot.

### 2.7 — Créer le bot Telegram (notifications téléphone)

1. Dans Telegram, parlez à **@BotFather** → `/newbot` → suivez les étapes → il vous donne un **token**.
2. Envoyez un message à votre nouveau bot, puis ouvrez dans un navigateur :
   `https://api.telegram.org/bot<VOTRE_TOKEN>/getUpdates`
   → repérez `"chat":{"id":...}` : c'est votre **chat_id**.
3. Mettez `bot_token` et `chat_id` dans `stores.json`, puis redémarrez le hub.

> Alternative sans Telegram : installez l'app **ntfy** sur votre téléphone, abonnez-vous à un sujet (ex. `difa-magasins-7f3a`), et mettez ce nom dans `ntfy_topic`.

---

## Étape 3 — Construire les exécutables (sur le magasin 1 ou un PC dev)

Les magasins 2 & 3 n'ont pas Python : on leur livre un `.exe` autonome.

```bat
python -m pip install pyinstaller
python -m pip install -r requirements_desktop.txt
install_windows\build_windows.bat
```

Résultat dans `dist\` :
- **`PrimeNFAgent.exe`** → à copier sur les magasins 2 & 3.
- **`PrimeNFHub.exe`** → l'application bureau (n'importe quel poste).

---

## Étape 4 — Magasins 2 & 3 (sans Python)

Sur **chaque** poste (2 puis 3) :

1. Copiez **deux fichiers** dans un dossier temporaire du poste :
   - `dist\PrimeNFAgent.exe`
   - votre `stores.json` rempli (le même que sur le hub)
   - et le dossier `install_windows\` (pour le `.bat`)
2. Ouvrez `cmd` **en Administrateur** dans ce dossier et lancez :
   ```bat
   install_windows\install_agent.bat 2      REM  magasin 2
   ```
   (sur le poste du magasin 3 : `install_agent.bat 3`)

Ce script **copie** l'exe + `stores.json` dans `C:\PrimeNFAgent\` et crée une **tâche planifiée** qui lance l'agent **toutes les 15 minutes**, avec l'option *« démarrer si l'horaire a été manqué »* (donc un cycle raté pendant la fermeture se rattrape au rallumage).

> Variante PowerShell équivalente :
> ```powershell
> powershell -ExecutionPolicy Bypass -File install_windows\setup_task.ps1 -StoreId 2
> ```

Test immédiat (sans attendre 15 min) :
```bat
C:\PrimeNFAgent\PrimeNFAgent.exe --store-id 2 --once
```

---

## Étape 5 — Application bureau (n'importe quel poste)

- **Poste avec Python** :
  ```bat
  python -m pip install -r requirements_desktop.txt
  python prime_hub.py
  ```
- **Poste sans Python** : copiez `PrimeNFHub.exe` + `stores.json` côte à côte et double-cliquez `PrimeNFHub.exe`.

L'app a 7 pages : Vue d'ensemble · Stock · Ventes · Trésorerie · **Import BDR** · **Éditeur de prix** · Synchronisation.

### Importer un Bon de Réception
1. Page **Import BDR** → choisissez le **magasin** cible (badge vert = en ligne, rouge = hors-ligne).
2. **Choisir fichier** (Excel `.xlsx` **ou** PDF `.pdf`) → **Prévisualiser**.
   - Pour un PDF, le texte est reconstruit automatiquement (chiffres arabes corrigés) ; une ligne dont `Qté × Prix ≠ Montant` est signalée.
3. **Importer** :
   - magasin **en ligne** → import direct immédiat ;
   - magasin **hors-ligne** → mis en file, appliqué à son prochain rallumage, **notification Telegram** à la clé.

### Modifier des prix
1. Page **Éditeur de prix** → cherchez l'article (réf. ou désignation).
2. Entrez le **nouveau prix HT** (TTC sera identique).
3. Cochez **les magasins** concernés (1, 2, 3 ou tous) → **Appliquer**.
   - en ligne → appliqué tout de suite ; hors-ligne → mis en file + notification au rallumage.

---

## Étape 6 (optionnelle) — Ajouter un PC d'édition (ex. votre PC perso, sans base)

L'application bureau est un **client du hub** : elle n'ouvre **aucune base** et
n'a **pas besoin de Firebird installé**. Toutes les lectures (tableaux de bord,
recherche d'articles) et toutes les écritures (import BDR, édition de prix)
passent par le hub (magasin 1). Vous pouvez donc l'installer sur **n'importe quel
PC** — votre PC personnel par exemple — pour travailler tranquillement.

Deux possibilités selon l'appareil :

**A. Appareil quelconque, avec un simple code d'accès (recommandé)**
Aucun `stores.json` à copier (donc **aucun mot de passe** ne quitte le hub) :

1. Installez **Tailscale** (même compte) → l'appareil rejoint le réseau.
2. Copiez juste **`PrimeNFHub.exe`** sur le PC.
3. Lancez-le : un écran de connexion demande
   - **l'adresse du hub** (ex. `http://100.x.y.z:5000`, l'IP Tailscale du magasin 1),
   - **le code d'accès** (`access_code` de `stores.json`, ex. `DIFA-2026`).
4. L'app récupère la liste des magasins depuis le hub et s'ouvre. Le code est
   **mémorisé** pour les lancements suivants (re-connexion automatique).

> Pour changer d'appareil ou se déconnecter : `PrimeNFHub.exe --logout`
> (ou changez l'`access_code` sur le hub pour révoquer tous les appareils).

**B. Poste géré (admin), avec stores.json**
Si vous préférez ne rien taper : copiez `PrimeNFHub.exe` **+** `stores.json`
côte à côte et double-cliquez — l'app se configure toute seule.

> Variante développeur (PC avec Python) :
> ```bat
> python -m pip install -r requirements_desktop.txt
> python prime_hub.py
> ```
> Le paquet `fdb` y figure mais **aucune installation de Firebird n'est requise** :
> l'app ne se connecte jamais à une base en local.

**Comment ça marche pour vous :**
- Vous voyez l'état des 3 magasins, le stock, les ventes, la trésorerie — en
  direct depuis le hub.
- Un **magasin en ligne** (le magasin 1 l'est toujours) → vos modifications de
  prix / imports BDR sont **appliquées immédiatement** par le hub.
- Un **magasin hors ligne** (2 ou 3 fermés le soir) → l'opération est **mise en
  file** sur le hub ; le magasin l'applique **tout seul** à son prochain
  démarrage, et vous recevez une **notification téléphone**.

> Si le magasin 1 (hub) est éteint ou injoignable, l'app s'ouvre mais reste vide
> (elle a besoin du hub pour toutes les données). Le magasin 1 étant « toujours
> allumé », ce n'est normalement jamais le cas.

---

## Étape 7 (optionnelle) — Éditer depuis le téléphone (sans app)

En plus de l'application bureau, le hub expose des **pages mobiles** dans le
navigateur du téléphone : importer un Bon de Réception (Excel **ou PDF**, ex.
une photo/scan) et modifier un prix, sans installer quoi que ce soit.

1. Installez **Tailscale** sur le téléphone (même compte), pour joindre le hub.
2. Ouvrez dans le navigateur : `http://100.x.y.z:5000/m/` (IP Tailscale du
   magasin 1).
3. Entrez le **code d'accès** (`access_code` de `stores.json`). La session
   reste ouverte 30 jours.
4. Depuis l'accueil mobile :
   - **📦 Importer un Bon de Réception** → choisir le magasin → envoyer le
     fichier → un aperçu des lignes s'affiche → **Confirmer**. Le hub lit le
     fichier lui-même (pas besoin d'app), applique tout de suite si le
     magasin est en ligne, sinon met en file (+ notification).
   - **💲 Modifier un prix** → chercher l'article → nouveau prix → cocher
     les magasins → **Appliquer**.
   - **💰 Voir la trésorerie** / **📊 Tableau de bord** → mêmes pages que sur
     ordinateur, adaptées à l'écran du téléphone.

> Astuce : sur iPhone/Android, utilisez « Ajouter à l'écran d'accueil » depuis
> le navigateur pour avoir une icône comme une vraie application.

> Sécurité : ces pages sont protégées par le `access_code` (session de 30
> jours). Elles ne sont accessibles que via le réseau Tailscale (l'IP
> `100.x.y.z`), pas depuis Internet.

---

## Étape 8 — Sauvegardes nocturnes (hub, fortement recommandé)

Sur le magasin 1, en Administrateur :

```bat
notepad install_windows\backup_hub.bat     REM adapter les variables en tête
install_windows\backup_hub.bat             REM test manuel : vérifier backups\
install_windows\setup_backup_task.bat      REM planifie chaque nuit à 02:00
```

Ce que la sauvegarde fait chaque nuit dans `backups\` :
- **`central_AAAA-MM-JJ.db`** — copie cohérente de la base centrale (API backup
  SQLite, sûre pendant que le hub tourne).
- **`DIFA2_AAAA-MM-JJ.fbk`** — sauvegarde à chaud de votre base Netfact2 via
  `gbak` (l'outil officiel Firebird ; la base reste utilisable pendant).
- Les fichiers de plus de **14 jours** sont supprimés automatiquement.

> Vérifiez le chemin de `gbak.exe` dans le script (installation Firebird 2.5).
> Restauration d'un `.fbk` : `gbak -c -user SYSDBA -password ... DIFA2_date.fbk C:\restaure\DIFA2.FDB`.

> **Connexion navigateur** : depuis la mise à jour sécurité, le tableau de bord
> web demande le même code d'accès que les pages mobiles (session 30 jours).

---

## Vérification (à faire AVANT de se fier au système)

Sur **chaque** poste qui a Python :
```bat
python verify.py --store-id 1        REM sur le hub
python verify.py --store-id 2        REM sur le poste 2
```
Sur un poste **sans** Python (magasin 2 / 3), l'agent lui-même sert de test :
```bat
C:\PrimeNFAgent\PrimeNFAgent.exe --store-id 2 --once
```

`verify.py` contrôle : `fdb` chargé, **connexion Firebird** (lit une désignation pour confirmer le charset arabe/français), **hub joignable**, **clé API**.

Scénarios à valider une fois :
1. **Backfill** → le tableau de bord `/` montre les données du magasin 1.
2. **Agent magasin 2** (`--once`) → les pages Stock/Ventes/Trésorerie montrent le magasin 2.
3. **Hors-ligne** : éteignez le magasin 2 → le tableau de bord garde ses dernières données avec « il y a Xh ».
4. **BDR hors-ligne** : import 2 lignes vers le magasin 2 éteint → rallumez-le → l'agent applique → message Telegram reçu → vérifiez dans Netfact2 (pièce `PC_AC_B`, stock augmenté).
5. **Prix sur les 3** : changez un prix scope « tous » → magasins en ligne appliqués tout de suite, magasin éteint appliqué au rallumage + notification.

---

## Dépannage rapide

| Symptôme | Cause probable | Solution |
|---|---|---|
| `verify.py` : Firebird échoue | mauvais chemin/mot de passe `DIFA2.FDB` | corrigez `database`/`password` dans `stores.json` |
| `verify.py` : hub injoignable | hub éteint ou mauvaise IP Tailscale | démarrez `hub_server.py` ; vérifiez `host` du hub |
| Magasin reste « hors-ligne » dans l'app | poste éteint, ou port 3050 bloqué | vérifiez Tailscale (`ping 100.x.y.z`) |
| Import PDF : « module pikepdf manquant » | dépendances PDF absentes | `python -m pip install pikepdf pdfplumber fonttools` (déjà dans l'exe) |
| `pip install X` dit que c'est installé, mais Python dit toujours que X est manquant | plusieurs Python sur le PC, `pip` et `python` ne pointent pas vers le même | utilisez **toujours** `python -m pip install X` (jamais `pip install X` seul) |
| `python` seul ne marche pas / installe dans le mauvais Python | plusieurs versions de Python installées | remplacez `python` par `py -3.11` **partout** (`py -0` liste vos versions) |
| Pas de notification téléphone | token/chat_id vides ou erronés | refaites l'étape 2.7 ; testez ntfy en secours |
| Tableau de bord vide | backfill non lancé / agents pas encore passés | `python backfill_store1.py` ; lancez un agent `--once` |

> **Antivirus** : ajoutez `central.db`, `central.db-wal`, `central.db-shm` aux exclusions Windows Defender sur le magasin 1 (sinon SQLite peut ralentir).

---

## Où sont les choses (aide-mémoire)

| Fichier / dossier | Rôle |
|---|---|
| `stores.json` | **votre config** (IPs Tailscale, bases, mots de passe, clés) — jamais sur git |
| `hub_server.py` | serveur central (magasin 1) |
| `sync_agent.py` / `PrimeNFAgent.exe` | agent de synchro (tous les postes) |
| `prime_hub.py` / `PrimeNFHub.exe` | application bureau |
| `central.db` | base centrale agrégée (sur le hub) |
| `src/vendor/bdr/` | outil Bon de Réception d'origine (Excel + PDF) |
| `src/vendor/editor/` | éditeur de prix d'origine |
| `install_windows/` | scripts d'installation Windows |
| `verify.py` | diagnostic |
