# Affichage Prix Netfact

Kiosque d'affichage de prix par code-barres pour les magasins utilisant le logiciel de gestion **Netfact2**. Le client scanne un article ; le poste kiosque affiche instantanément le nom du produit, son prix en dinars algériens (DA) et — si WooCommerce est configuré — sa photo.

---

## Présentation

L'application est conçue pour fonctionner sur un écran tactile ou un simple moniteur en libre-service. Elle :

- se connecte en réseau local à la base de données **Firebird 2.5** de Netfact2 ;
- recherche l'article par **code-barres** (champ `CODE_BARRES` ou `CODE_BARRE`) ou par **référence article** (`REF_ART`) ;
- affiche en plein écran le **nom** et le **prix** de l'article en quelques dixièmes de seconde ;
- télécharge et met en cache la **photo produit** depuis WooCommerce (optionnel) ;
- revient automatiquement à l'écran d'accueil après quelques secondes d'inactivité ;
- démarre automatiquement avec Windows grâce à `install_autostart.bat`.

L'interface est bilingue **français / arabe**.

---

## Prérequis — côté serveur Netfact

### Service Firebird 2.5

Le serveur doit faire tourner **Firebird 2.5** (SuperServer ou SuperClassic) en tant que service Windows.

Pour vérifier : `services.msc` → chercher « Firebird Server » ou « Firebird Guardian ».

### Ouverture du port TCP 3050

Le poste kiosque communique avec le serveur via le port **TCP 3050**. Sur le serveur, ouvrez ce port dans le pare-feu Windows :

```
Pare-feu Windows Defender avec sécurité avancée
→ Règles de trafic entrant → Nouvelle règle...
→ Port → TCP → Port local : 3050
→ Autoriser la connexion → Profil : Domaine + Privé
→ Nom : Firebird 2.5
```

### Informations nécessaires

| Information | Exemple |
|---|---|
| Nom NetBIOS du serveur ou IP | `SERVEUR` ou `192.168.1.15` |
| Chemin du fichier `.FDB` (côté serveur) | `C:\Netfact\Data\PR22.FDB` |
| Utilisateur Firebird | `SYSDBA` |
| Mot de passe SYSDBA | `masterkey` (défaut à l'installation) |

---

## Prérequis — côté poste kiosque

- **Windows 10 ou 11** (32 ou 64 bits selon Python installé).
- **Accès réseau LAN** au serveur Netfact (test : `ping SERVEUR`).
- **Accès Internet** (ou intranet) pour récupérer les photos WooCommerce (optionnel).
- **`fbclient.dll`** du client Firebird 2.5 (voir ci-dessous).

### Client Firebird (`fbclient.dll`)

La DLL cliente doit être présente soit :

- **Option A** : installer le [client Firebird 2.5](https://firebirdsql.org/en/firebird-2-5/) sur le poste kiosque (cocher « Client component »).
- **Option B** : copier `fbclient.dll` depuis le serveur (dossier `bin\` de l'installation Firebird, généralement `C:\Program Files\Firebird\Firebird_2_5\bin\fbclient.dll`) et le placer **à côté de `AffichagePrix.exe`** dans `dist\`.

Assurez-vous que la version de la DLL correspond à l'architecture (32 bits si Python 32 bits, 64 bits sinon).

---

## WooCommerce — configuration des clés API

Si votre boutique WooCommerce est synchronisée avec Netfact (le SKU WooCommerce = `REF_ART` dans Netfact), l'application peut afficher les photos produits.

1. Connectez-vous à l'administration WordPress.
2. Allez dans **WooCommerce → Réglages → Avancé → REST API**.
3. Cliquez sur **Ajouter une clé**.
4. Description : `Kiosque prix`, Utilisateur : votre admin, Permissions : **Lecture**.
5. Copiez la **clé API** (`Consumer key`) et le **secret** (`Consumer secret`).
6. Renseignez ces informations dans l'assistant de configuration au premier démarrage.

---

## Installation et premier lancement

### Depuis l'exécutable (poste kiosque sans Python)

1. Copiez le dossier `dist\` sur le poste kiosque (ou transférez `AffichagePrix.exe`).
2. Assurez-vous que `fbclient.dll` est à côté de l'exe (voir ci-dessus).
3. Lancez `AffichagePrix.exe`.
4. **L'assistant de configuration** s'ouvre automatiquement au premier démarrage. Renseignez les informations de connexion et cliquez sur **Tester la connexion** pour valider.
5. Cliquez sur **Enregistrer et démarrer** : le kiosque s'ouvre en plein écran.

### Démarrage automatique avec Windows

Lancez `install_autostart.bat` (en tant qu'utilisateur courant, pas en administrateur) : il crée un raccourci dans le dossier **Démarrage** de Windows. Le kiosque se lancera automatiquement à chaque ouverture de session.

### Raccourcis clavier (pendant l'utilisation)

| Raccourci | Action |
|---|---|
| `Ctrl+Alt+Q` | Quitter l'application |
| `Ctrl+Alt+S` | Rouvrir l'assistant de configuration |

Ces raccourcis sont personnalisables dans `config.ini` (paramètre `exit_hotkey`).

---

## Développement

### Installation des dépendances

```bash
pip install -r requirements.txt
```

> **Note** : `fdb` requiert que le client Firebird soit installé ou que `fbclient.dll` soit accessible. Sur Linux, des solutions alternatives existent (non prises en charge dans ce projet).

### Lancement en mode développement

```bash
python src/main.py
```

### Test de connexion sans interface graphique

```bash
python src/main.py --selftest
python src/main.py --selftest --code 1234567890
```

Codes de retour : `0` = succès DB, `1` = échec DB, `2` = configuration incomplète.

---

## Construction de l'exécutable Windows

```bat
build.bat
```

Ce script utilise **PyInstaller** pour produire `dist\AffichagePrix.exe` (exécutable autonome, sans console).

Prérequis : `pip install pyinstaller`.

Pour une icône personnalisée, placez `assets\app.ico` avant de lancer `build.bat`.

Après la compilation, copiez `fbclient.dll` dans `dist\` si le client Firebird n'est pas installé sur le poste cible.

---

## Dépannage — connexion réseau

| Symptôme | Cause probable | Solution |
|---|---|---|
| `Unable to complete network request to host` | Serveur injoignable | Vérifier `ping SERVEUR`, port 3050 ouvert dans le pare-feu |
| `connection rejected by remote interface` | Service Firebird arrêté | Démarrer le service Firebird sur le serveur |
| `Your user name and password are not defined` | Mauvais identifiants | Vérifier SYSDBA / mot de passe |
| `I/O error … file not found` | Chemin FDB incorrect | Vérifier le chemin côté serveur |
| `Unable to load fbclient library` | DLL manquante | Installer le client Firebird ou copier `fbclient.dll` |
| Nom de PC non résolu | DNS/NetBIOS défaillant | Utiliser l'adresse IP à la place du nom |

### Conseil : nom de PC vs adresse IP

Si le nom NetBIOS du serveur (ex : `SERVEUR`) ne fonctionne pas, utilisez directement son **adresse IP** dans la configuration. Vous pouvez la trouver avec `ipconfig` sur le serveur.

### Vérification du port 3050

Depuis le poste kiosque (PowerShell) :

```powershell
Test-NetConnection -ComputerName SERVEUR -Port 3050
```

Si `TcpTestSucceeded` est `False`, le port n'est pas accessible (pare-feu ou service arrêté).
