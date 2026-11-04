# Daemon Harness

[English](../README.md) · **Français**

**Daemon Harness** est un système cognitif open source pour un assistant personnel
IA : votre **daemon**. C’est lui qui mène vos conversations, avec une identité,
une mémoire et une continuité qui se construisent au fil de vos échanges.

Il s’appuie sur un **agent hôte**, Hermes Agent ou OpenClaw, qui lui apporte le
modèle, le harness et les canaux de communication. Votre daemon fonctionne
ainsi sur un double harness, deux couches : Daemon Harness, qui porte son
identité, sa mémoire et sa continuité, et l’hôte, qui lui donne ses capacités
d’action. Son identité et sa mémoire n’appartiennent ni à l’hôte ni au
modèle : elles sont conçues pour survivre à un changement de l’un ou de
l’autre, au fil des années.

Votre daemon conserve ce qui compte, relie les événements et suit l’évolution
de vos projets, de vos préférences et de votre situation. À chaque message, il
retrouve le contexte utile, sans repartir de zéro ni réciter tout ce qu’il sait.

## Les piliers d’un assistant personnel

Daemon Harness part d’une conviction : un très bon assistant personnel repose
sur six piliers. Voici comment Daemon Harness répond à chacun.

**Le contexte.** Pour vous aider, un assistant doit comprendre votre
situation : savoir si vous êtes au travail, chez vous, en voyage, en réunion,
au sport ou en famille. Plus le contexte est riche, plus l’aide est utile et
personnelle. Votre daemon tient une lecture datée et révisable de votre
situation présente (ce qui est établi, ce qui n’est que supposé, les échéances
à venir), mise à jour après chaque échange à partir de ce que vous lui confiez.

**La mémoire.** Au fil du temps, un assistant apprend vos préférences, vos
méthodes de travail, vos habitudes, vos relations, vos routines et vos
conversations passées. Votre daemon les conserve dans des notes Markdown
locales, rangées en cinq dossiers ([PARGA](parga.fr.md)), chaque fait gardant
sa source. Pour répondre, il ne lit que ce qui concerne le sujet.

**La personnalité.** Une bonne conversation est fluide et naturelle, avec un
ton et une manière d’échanger qui conviennent à la personne, comme avec un ami
de longue date. Votre daemon garde une seule voix, stable ; sa personnalité et
sa façon de converser sont décrites dans deux fichiers que vous pouvez
modifier. Aucun second assistant ne réécrit ses réponses, et la préparation du
contexte n’ajoute aucun appel au modèle avant la réponse.

**Les compétences.** Un assistant doit aussi agir : rechercher, programmer,
planifier, analyser des finances, utiliser des outils de conception, organiser
un voyage, suivre la santé ou coopérer avec d’autres agents spécialisés.
Daemon Harness confie cette part à un hôte conçu pour cela : Hermes Agent ou OpenClaw,
d’autres à venir. L’hôte garde ses propres outils et autorisations.

**La confiance.** On ne s’appuie que sur un assistant fiable et juste : il dit
ce qu’il sait, reconnaît ce qu’il ignore et vérifie avant d’affirmer. Votre
daemon est conçu pour distinguer ce dont il se souvient de ce qu’il suppose, et
pour vérifier dans ses notes avant d’affirmer un fait : une recherche vide ne
prouve pas qu’une chose n’a pas eu lieu, et un souvenir ancien ne décrit pas le
présent.

**La continuité.** Un assistant personnel s’envisage sur des années : il ne
doit dépendre ni d’un seul fournisseur de modèle, ni d’un agent, ni d’un
hébergeur. L’identité et la mémoire de votre daemon vivent sur votre
ordinateur, hors du modèle et de l’hôte. Un daemon créé avec l’app peut passer
d’Hermes Agent à OpenClaw, et inversement, avec sa mémoire ; une sauvegarde
chiffrée le déplace sur un autre ordinateur.

### Votre mémoire, aussi dans Obsidian

Votre mémoire vous appartient. Elle reste sur votre ordinateur, dans des
fichiers Markdown que vous pouvez lire et modifier directement ou dans
[Obsidian](https://obsidian.md). [Votre mémoire et Obsidian](memory.md) (en anglais).

Le projet est en **alpha** : consultez les [capacités et limites](alpha-testing.md).
Le stockage local ne signifie pas que le modèle s’exécute sur votre ordinateur :
le contexte pertinent est transmis au fournisseur de modèle choisi.

## Installer l’app

L’app **Daemons Companion** installe et gère vos daemons sur votre ordinateur.
Elle permet de :

- créer un ou plusieurs daemons, chacun avec son hôte, Hermes Agent ou
  OpenClaw, que l’app installe, avec sa connexion au modèle et son bot Telegram ;
- ajouter OpenClaw à un daemon Hermes Agent, avec la même mémoire, ou faire
  passer un daemon d’un hôte à l’autre ;
- ouvrir la mémoire de votre daemon, la sauvegarder chiffrée dans un fichier ou
  sur Google Drive, et la restaurer pour déménager votre daemon sur un autre
  ordinateur.

Ces commandes conviennent aux **Mac Apple Silicon** et à **Windows x64**.
Elles restent identiques et téléchargent la version **alpha actuellement publiée**,
y compris lorsqu’elle porte le statut de préversion sur GitHub.
Le dépôt demande actuellement un compte GitHub autorisé.

### Préparer GitHub CLI une seule fois

Sur Mac, suivre les [instructions macOS](https://github.com/cli/cli/blob/trunk/docs/install_macos.md).
Sur Windows, exécuter dans PowerShell :

~~~powershell
winget install --id GitHub.cli --source winget
~~~

Sur Windows, fermer toutes les fenêtres PowerShell / Windows Terminal,
puis rouvrir PowerShell depuis Démarrer. Vérifier `gh --version`.
Sur Mac comme sur Windows, connecter le compte autorisé :

~~~sh
gh auth login --hostname github.com --web --git-protocol https
~~~

### Mac — Terminal

~~~sh
(set -e; daemons_script=$(mktemp); trap 'rm -f "$daemons_script"' EXIT; gh api 'repos/jpacifico/daemon-harness/contents/apps/companion/distribution/install-macos.sh?ref=main' -H 'Accept: application/vnd.github.raw+json' > "$daemons_script"; sh "$daemons_script")
~~~

### Windows — PowerShell

~~~powershell
$daemonsScript = gh api 'repos/jpacifico/daemon-harness/contents/apps/companion/distribution/install-windows.ps1?ref=main' -H 'Accept: application/vnd.github.raw+json'; if ($LASTEXITCODE -eq 0) { & ([scriptblock]::Create(($daemonsScript -join "`n"))) }
~~~

Fermez l’app avant de relancer la commande pour la remplacer.
À partir de r10, l’app se met aussi à jour elle-même : **Réglages > Rechercher une
mise à jour**. Remplacer ou mettre à jour l’app ne réinstalle ni ne met à
niveau votre daemon existant. Quand l’app apporte des changements à la façon
dont elle installe et fait fonctionner les daemons, la page du daemon propose
**Mettre à jour** ; voir l’[impact de l’installation](installation_impact.md#updating-a-daemon).
Les paquets ne disposent pas encore de signatures de distribution reconnues.
Le [guide d’installation](companion_installation.md) explique les difficultés
connues et les solutions si `gh` n’est pas reconnu.

## Commencer

1. Ouvrir l’app et choisir **Language → Français** si nécessaire.
2. Choisir **Installer un daemon**, puis Hermes Agent (recommandé) ou OpenClaw.
3. Nommer le daemon, choisir son genre grammatical et la langue de conversation,
   puis connecter un accès au modèle.
4. Suivre la création du bot Telegram et appuyer sur **Démarrer** : le daemon se
   présente et apprend comment vous appeler.

Le parcours crée une **installation privée et séparée de l’hôte choisi**. Les deux
hôtes acceptent la connexion avec un abonnement ChatGPT, recommandée avec Hermes
Agent, ou une clé API OpenAI ou Anthropic. **Ajouter un hôte** peut
ensuite ajouter OpenClaw à un daemon Hermes Agent, avec la même mémoire.
L’ajout d’un daemon sur un hôte existant quelconque n’est pas un parcours guidé.
Les modèles proposés, et pourquoi, sont listés dans [Fournisseurs de modèles](model_providers.fr.md).

## Effets de l’installation

Après confirmation du plan d’installation, l’app :

- installe un **hôte privé** : sa propre copie de Hermes Agent ou d’OpenClaw,
  réservée à votre daemon et séparée de toute copie déjà présente sur votre
  ordinateur, à laquelle elle ne touche pas ;
- crée le dossier memory de votre daemon, ses services en arrière-plan (le
  Gateway de l’hôte, l’entretien de la mémoire après chaque réponse, un
  entretien nocturne) et un modèle de recherche local d’environ 640 Mo ; les
  clés API vont dans le stockage d’identifiants du système ;
- laisse à l’hôte ses outils, mais désactive sa propre mémoire personnelle,
  qui ferait concurrence à celle du daemon ;
- fait demander par l’hôte votre accord, dans votre conversation Telegram,
  avant de lancer une commande sur votre ordinateur : sur OpenClaw, toute
  commande que vous n’avez pas encore autorisée ; sur Hermes Agent, les
  commandes qu’il juge dangereuses ;
- laisse désactivés le mode arrière-plan, l’ouverture à la connexion, la
  sauvegarde chiffrée et Google Drive tant que vous ne les activez pas ;
- ne supprime jamais le dossier memory : retirer un daemon ou son hôte le
  conserve.

Les [effets exacts](installation_impact.md) (en anglais) détaillent les chemins,
les versions, les réglages, l’entretien, le retrait et les limites selon la
plateforme.

## Ce qui a été mesuré

Les bancs d’essai existants évaluent les compétences d’un agent, pas sa
connaissance de la personne qu’il assiste. Nous avons donc construit le
[Continuity Benchmark](continuity-benchmark.fr.md) : il compare le même Hermes
Agent, avec le même modèle et les mêmes outils, avec et sans le daemon, sur 24
personnes fictives qui parlent chacune à leur assistant au fil de plusieurs
conversations. Ses critères ont été fixés avant toute exécution, et deux juges
Claude Opus 5.5 à l’aveugle doivent s’accorder sur chaque verdict.

Version 1.1 (31 octobre 2026) :

| | Hermes seul | Hermes + daemon |
| --- | ---: | ---: |
| Utilise tous les faits antérieurs | 12 / 24 | **16 / 24** |
| Redemande une chose déjà dite | 2 | **0** |
| Utilise un contexte périmé ou hors sujet | 5 | **1** |
| Invente un fait | 0 | 0 |
| Préféré en comparaison à l’aveugle | 8 | **13** (3 égalités) |
| Tâches d’agent natives réussies (version 1.0) | 12 / 12 | 12 / 12 |

Mesuré avec GPT-6-Astra via un abonnement ChatGPT. Un tour complet consomme
actuellement 3,42 fois les tokens de Hermes seul, surtout pour l’entretien de
la mémoire après la réponse ; nous travaillons à la réduire et publierons nos
avancées dès que possible.

[Résultats complets, méthode et limites](continuity-benchmark.fr.md).

## Comprendre et contribuer

L’[index documentaire](README.md) donne accès aux guides d’utilisation et aux
références développeur : architecture, configuration, API, mémoire,
intégrations, exploitation, dépannage et évaluations.
[Le daemon en un coup d’œil](overview.fr.md) montre un tour
complet en un schéma, puis explique ce que la voix reçoit et comment la mémoire
est entretenue. L’[architecture](architecture.fr.md) décrit en détail chaque
composant, chaque étape d’un tour et chaque règle.
Les termes internes sont définis dans le [glossaire](glossary.md).
[PARGA : le classement de la mémoire](parga.fr.md) présente les cinq dossiers
de la mémoire.
Le [guide développeur de la mémoire](memory_structure_developer_guide.md)
en détaille les formats et le code.

Créé par **Jonathan Pacifico**.
[Contact via GitHub](https://github.com/jpacifico/daemon-harness/issues) ·
[Licence MIT](../LICENSE).
