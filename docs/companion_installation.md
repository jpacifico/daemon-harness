# Installer Daemons Companion

Présentation du projet et installation

La Companion s’installe sur **Mac Apple Silicon** (M1 et suivants) ou
**Windows x64**. Aucun compte n’est nécessaire pour la télécharger.

Pour installer un nouveau daemon, prévoyez aussi un accès ChatGPT/Codex ou une
clé OpenAI ou Anthropic, et un compte Telegram. L’app guide la création d’un bot
dédié et prépare ses dépendances : aucun Python, Hermes ou Codex à installer
manuellement. Ces connexions ne sont pas à refaire pour remplacer seulement l’app.

L’app n’a pas encore de signature de distribution reconnue. Utilisez les
commandes ci-dessous, qui récupèrent et vérifient la version publiée courante.

## Installer par commande

Ces commandes téléchargent la Companion, vérifient son empreinte, l’installent
puis l’ouvrent. **Elles restent identiques à chaque version** : à chaque
exécution, le script courant récupère la dernière Companion publiée et vérifie
son empreinte SHA-256. Aucun numéro de version à changer dans la commande.
Ne collez aucun mot de passe ou jeton dans les commandes.

### Mac — Terminal

Lancer cette commande dans Terminal, sans `sudo` :

```sh
(set -e; daemons_script=$(mktemp); trap 'rm -f "$daemons_script"' EXIT; curl --proto '=https' --tlsv1.2 -fsSL 'https://raw.githubusercontent.com/jpacifico/daemon-harness/main/apps/companion/distribution/install-macos.sh' -o "$daemons_script"; sh "$daemons_script")
```

L’app est installée dans `~/Applications/Daemons Companion.app`.

### Windows — PowerShell

Lancer cette commande dans **PowerShell**, dans votre session habituelle, sans
droits administrateur :

```powershell
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072; & ([scriptblock]::Create((Invoke-RestMethod -UseBasicParsing -ErrorAction Stop 'https://raw.githubusercontent.com/jpacifico/daemon-harness/main/apps/companion/distribution/install-windows.ps1')))
```

L’app est installée dans
`%LOCALAPPDATA%\Programs\Daemons Companion\Daemons Companion.exe`.
La commande ne modifie pas la stratégie d’exécution de PowerShell.

## Ouvrir l’app et commencer

La Companion démarre en anglais. **Réglages** permet de changer la langue,
l’apparence et de rechercher une mise à jour de l’app. Ces préférences sont
conservées à la réouverture. Depuis **Mes daemons**, **Installer un daemon**
ouvre la configuration guidée.

**À propos** explique la continuité personnelle et la mémoire locale. Son dossier
peut être ouvert comme un coffre Obsidian pour lire, corriger et organiser les
mêmes fiches que celles utilisées par le daemon, sans export intermédiaire.

Le parcours de création propose **Hermes Agent** (recommandé) ou **OpenClaw**,
puis **ChatGPT**, une clé **OpenAI API** ou **Anthropic API**.
Il demande le nom du daemon, son genre grammatical et la langue de conversation ;
le daemon apprend ensuite dans la conversation comment vous appeler. Avec Hermes
Agent, l’abonnement ChatGPT est recommandé ; avec OpenClaw, les clés API sont
proposées en premier. Pour une clé API, l’app récupère les modèles accessibles et
vérifie le modèle sélectionné avant Telegram. Les modèles proposés, et pourquoi,
sont listés dans [Fournisseurs de modèles](model_providers.fr.md). La clé sert à la conversation et à
l’entretien de la mémoire ; elle est conservée dans le coffre du système. Aucun
Codex à installer sur le parcours API. Le quota et la facturation dépendent du
fournisseur et du compte associé à la clé. Sur le parcours ChatGPT, les connexions
de la conversation et de l’entretien sont distinctes : l’app explique les deux étapes.

Pour Telegram, suivre le guide dans l’app depuis l’ordinateur, créer un bot dédié
avec BotFather, puis coller le jeton dans le champ masqué. Appuyer sur
**Démarrer** dans la conversation du bot : le daemon se présente. Si le bouton
ne réagit pas dans Telegram Web A, utiliser **Telegram Web K** ou rechercher
le `@nom` exact du bot dans Telegram.

L’app s’ouvre sur **Mes daemons**. Le nom du daemon apparaît en premier,
avec son ou ses hôtes en dessous. Les hôtes qui utilisent le même runtime de
mémoire vérifié sont regroupés ; un nom identique ne suffit pas. Les hôtes sans
daemon sont présentés séparément, en bas de la liste.

**Installer un daemon** crée une installation privée et séparée de l’hôte choisi,
même sans hôte installé ou avec une version existante non prise en charge.
**Ajouter un hôte** ajoute OpenClaw à un daemon Hermes Agent créé par la
Companion, avec sa propre connexion au modèle, son propre bot Telegram et la même
mémoire. **Ajouter une installation existante** reste une action distincte.
Chaque installation inachevée dispose de **Reprendre l’installation** et de son
propre diagnostic lorsqu’un rapport a été enregistré.

## Gérer un daemon déjà installé

Votre daemon est votre assistant personnel IA. Hermes Agent ou OpenClaw est
son **agent hôte** : il lui fournit ses outils et ses moyens d’action.

Dans **Mes daemons**, choisissez **Gérer** sur le daemon concerné. S’il utilise
plusieurs hôtes, choisissez celui dont vous souhaitez gérer les préférences de compte.
Vous pouvez modifier le nom et l’accord grammatical du daemon et, si sa version
le permet, le prénom ou surnom qu’il utilise pour vous.
Cliquez sur **Enregistrer les modifications**. Sur Mac, une progression puis une
confirmation restent visibles près du bouton. Une nouvelle saisie réactive
l’enregistrement ; une erreur conserve vos modifications et permet de réessayer.
Aucun redémarrage n’est nécessaire.
Ces préférences complètent la configuration : une correction explicite déjà
donnée en conversation reste prioritaire.

La partie **Mémoire** affiche le dossier réellement utilisé. **Ouvrir le dossier
mémoire** l’ouvre dans le Finder ou l’Explorateur ; **Copier le chemin** permet de
le retrouver dans Obsidian et de l’y ouvrir comme coffre. Le déplacement du
dossier depuis l’app est prévu après la V0.

Sur Mac, **Comptes et canaux** liste les identités détectées sur les hôtes
partageant cette mémoire et indique le compte auquel les préférences
s’appliquent. Cette liste ne rattache pas de compte supplémentaire : le
raccordement de plusieurs canaux sera ajouté séparément.

Si plusieurs hôtes utilisent la même mémoire, le formulaire les signale et
l’enregistrement demande confirmation. Une version ancienne sans prise en charge
des préférences d’adresse affiche cette limite : remplacer la Companion ne met
pas à jour le code du daemon. Le contrôle des services de l’hôte reste une action
séparée ; ouvrir ces réglages ne démarre et n’arrête aucun service.

## Remplacer une app existante

La Companion se met à jour elle-même : elle vérifie peu après son ouverture
puis toutes les six heures, et **Réglages > Rechercher une mise à jour**
vérifie aussitôt. Relancer la commande d’installation remplace aussi l’app.

Pour remplacer l’app à la main, fermer la Companion avant de relancer la commande
d’installation ou d’ouvrir le nouveau paquet. Il n’est pas nécessaire de
désinstaller l’app au préalable.
Le remplacement conserve le daemon, ses connexions et sa mémoire ; il ne met pas
à jour Hermes, OpenClaw ou le code du daemon déjà installé. Quand la nouvelle app
apporte des changements à la façon dont elle installe et fait fonctionner les
daemons, la page du daemon propose **Mettre à jour**. Le moteur du daemon, sa
mémoire, sa connexion au modèle et son bot Telegram ne changent pas ; si une
étape échoue, la version précédente est rétablie à l’identique.

Sur Mac, la commande prépare la nouvelle app avant de remplacer la précédente
et restaure l’ancienne si ce remplacement échoue. Sur Windows, le remplacement
est géré par l’installateur de l’app. Une installation dans un autre emplacement
peut être refusée plutôt que déplacée silencieusement.

## Ce qui est installé

La création d’un daemon prépare une nouvelle installation de l’hôte choisi dans
`DaemonHarness/Companion/instance-<id>`, sous Application Support sur Mac ou
LocalAppData sur Windows. Les installations personnelles déjà présentes ne sont
pas reprises automatiquement. Les anciennes installations Companion dans
`Companion/private` restent utilisables. Les connexions et le jeton Telegram restent dans
les fichiers privés de cette installation.

La mémoire comporte cinq dossiers à sa racine : `Projects`, `Areas`, `Resources`,
`Garden`, `Archives`. Les journaux et index internes sont masqués dans `.daemon`.
Les souvenirs sont des fichiers Markdown locaux ; le contexte nécessaire aux
réponses et à leur entretien est envoyé au fournisseur de modèle connecté.

Le daemon utilise des services de conversation, d’entretien après les échanges,
de traitement nocturne et de recherche locale (modèle d’environ 640 Mo). L’ordinateur doit rester allumé et connecté pour
répondre ; les tâches Windows fonctionnent dans la session utilisateur.

Dans **Mes daemons → Détails de l’hôte**, le raccordement explicite autorise la Companion à démarrer,
arrêter ou redémarrer le service reconnu de l’installation choisie. Il ne
réinstalle rien. Les services de mémoire partagée restent actifs. **Oublier**
retire seulement le raccordement dans l’app, sans supprimer les fichiers.

## Limites et aide

L’app reste une version d’essai. Le paquet Mac n’est pas notarisé et le paquet
Windows n’est pas signé : une alerte système peut apparaître. Aucun script
ne désactive les protections de l’ordinateur. Si le lancement est bloqué,
conserver le message exact pour le signaler.

L’installation d’un daemon sur un hôte déjà présent, l’ajout de Hermes Agent à
un daemon OpenClaw et les mises à jour du daemon depuis l’app restent à venir.
Le retrait est disponible sur Mac et Windows pour les daemons Hermes Agent créés
par la Companion ; voir la section Désinstaller ci-dessous. Un daemon créé
directement sur OpenClaw se désinstalle avec son OpenClaw privé, en conservant
le dossier memory. Les machines distantes,
conteneurs, Mac Intel et Windows ARM ne sont pas couverts par ce parcours.

Un téléchargement refusé peut venir d’un compte GitHub sans accès au dépôt.
Pour signaler un autre problème, indiquer le système et le message d’erreur
dans les [issues GitHub](https://github.com/jpacifico/daemon-harness/issues),
sans joindre de jeton, fichier `.env`, connexion ou mémoire personnelle.

## Références techniques

- Gestion du daemon : contrôles macOS et Windows
- Validation de l’app et limites des essais
- Vérifications de l’installation par commande
- Passation pour les développeurs
- Historique de l’installation et anciennes procédures
- Anciennes notes propres à la branche Companion
- Intégrations : [Hermes](../integrations/hermes_plugin/INSTALL.md) · [OpenClaw](../integrations/openclaw_plugin/INSTALL.md)


<a id="détacher-ou-désinstaller"></a>
## Désinstaller

Sur Mac et Windows, ouvrir **Mes daemons → Détails de l’hôte → Désinstaller** sur la carte concernée.

- **Désinstaller Hermes et le daemon** arrête les deux et retire leurs programmes,
  leurs services et le modèle de recherche locale. Un OpenClaw ajouté à ce daemon
  est désinstallé aussi. Cette installation ne répondra plus sur Telegram. Pour confirmer,
  saisir le nom complet de l’installation affichée, par exemple **Hermes - Nova**.
- **Retirer le daemon, garder Hermes** retire l’intégration Daemon Harness, son
  entretien et le modèle de recherche locale. Un OpenClaw ajouté est déconnecté
  et reste installé. Hermes reste installé et peut continuer à répondre sur Telegram,
  sans utiliser la mémoire ni entretenir la continuité Daemon Harness. Pour confirmer,
  saisir le nom du daemon, par exemple **Nova**.

Après le retrait du daemon seul, la carte propose uniquement **Désinstaller
Hermes**. Le profil, les conversations, les connexions locales et les diagnostics
sont conservés dans les dossiers affichés ; ce n’est pas un effacement de compte.

Dans les deux cas, **memory est conservé à son emplacement actuel**, avec
**Ouvrir le dossier** et **Copier le chemin** avant et après. Pour effacer ses
souvenirs, l’utilisateur devra supprimer lui-même ce dossier. L’app ne le déplace
pas et ne propose pas de le supprimer. Le bot Telegram et les comptes fournisseurs
distants restent également intacts.

Le retrait automatique est limité aux installations Hermes complètes créées
par Companion, avec reçus vérifiables. Les mises à niveau privées dont le
manifeste courant est valide sont reconnues. Une installation externe, un
partage de services ou de mémoire, des chemins déplacés manuellement ou des
relances Hermes encore dépendantes du daemon peuvent bloquer l’action. Un daemon
créé directement sur OpenClaw n’est pas encore couvert par ce retrait.

En cas d’échec, l’app indique si elle a restauré l’état précédent ou s’il faut
reprendre le retrait. Dans ce dernier cas, revenir à la même carte, rouvrir la
même action et confirmer la reprise. Ne pas réinstaller dans un dossier dont
le retrait reste incomplet. Une modification concurrente reste conservée ;
elle peut nécessiter une vérification manuelle avant la reprise.

## Abandonner une installation inachevée

**Arrêter l’installation** conserve les composants téléchargés pour reprendre plus
tard. Pour abandonner définitivement, attendre l’arrêt, ouvrir **Mes daemons**,
puis choisir **Supprimer l’installation inachevée** sur sa carte et confirmer.
Ses programmes privés et ses services vérifiés sont retirés. Un daemon terminé
utilise l’action Désinstaller.

La mémoire, les profils locaux, les connexions enregistrées et les diagnostics
sont conservés. Leur dossier est affiché avant la confirmation ; les comptes
fournisseurs et le bot Telegram ne sont pas supprimés. Si le nettoyage échoue,
la carte et le diagnostic restent et l’on peut réessayer. Ne pas supprimer à la
main des programmes tant que le nettoyage signale un service en cours. Une
installation OpenClaw inachevée lancée depuis **Ajouter un hôte** se reprend
depuis cet écran ; elle ne se supprime pas. Voir la
version actuelle de l’app.
