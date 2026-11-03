# Le Daemon En Un Coup D'Œil

[English](overview.md) · **Français**

## L'idée

Un **daemon** est un assistant personnel IA qui se souvient de vous d'une
conversation à l'autre. Il ne fonctionne pas seul : il s'installe sur un
**hôte**, un programme d'agent conversationnel comme Hermes Agent ou OpenClaw.
L'hôte fournit le **modèle** (le grand modèle de langage, ou LLM, qui écrit
les réponses), le **canal** (par exemple Telegram), les sessions, l'affichage
progressif des réponses et ses propres **outils**, par exemple la recherche
Web ou l'accès aux fichiers.

Le daemon apporte ce que l'hôte n'a pas :

- une **voix stable** : un nom, une personnalité et une manière de converser ;
- une **mémoire personnelle**, écrite en Markdown (un format de texte simple)
  dans des fichiers que vous pouvez lire et modifier ;
- une **lecture de la situation présente**, mise à jour après chaque échange.

Le daemon ne parle jamais à la place de l'hôte. Il prépare ce que le modèle
lit avant de répondre, lui donne deux outils pour consulter la mémoire, puis
entretient cette mémoire après la réponse.

```text
Hôte (Hermes Agent ou OpenClaw)
  canal · session · modèle · affichage · outils externes

Daemon
  identité · mémoire Markdown · situation présente · entretien
```

## Les mots à connaître

| Terme | Ce que c'est |
| --- | --- |
| **L0** | Le niveau brut de l'historique de conversation : chaque message, de vous comme du daemon, copié mot pour mot avec sa date. L0 signifie *Level 0* ; on l'appelle aussi *Ground Zero*. Rien n'y est résumé ni réécrit. |
| **Souvenir** | Une note Markdown de la mémoire qui garde un fait personnel durable : une personne, un projet, une préférence, une situation. Chaque souvenir indique d'où vient ce qu'il affirme. |
| **Scène** | Une note de travail qui décrit la situation présente : ce qui est établi, ce qui est seulement supposé, ce qui reste incertain, et les échéances proches. Elle est réécrite après chaque échange et cite ses sources. Dans le code, elle s'appelle *Scene Model*. |
| **Index des souvenirs** | Un moteur de recherche local qui trouve les souvenirs proches du sujet de la conversation. Il compare des *embeddings*, des représentations numériques du sens d'un texte, calculées sur votre ordinateur. |
| **Après-tour** | Le travail d'entretien de la mémoire fait juste après chaque réponse, sans faire attendre la conversation. Les tâches attendent dans une **file** et sont traitées par un **worker**, un programme séparé qui tourne en arrière-plan. |
| **Radar**, **Temporal Curator**, **MemoryAgent** | Les trois jugements de l'après-tour, faits par un modèle. Le Radar relit l'échange et décide quel entretien il mérite ; Temporal Curator tient l'agenda personnel ; MemoryAgent crée ou complète les souvenirs. Ils sont détaillés plus bas. |
| **Dreaming**, **REM** | Les deux tâches d'entretien de la nuit. Dreaming relit et corrige les souvenirs récents ; REM écrit le récit d'épisodes passés. Ce sont des noms de tâches, pas des agents supplémentaires. |

## Un tour complet

```text
VOTRE MESSAGE
        │
        ├──→ copie exacte dans L0
        │
        └──→ PRÉPARATION LOCALE, SANS APPEL AU MODÈLE
                │
                ├─ consignes principales
                │    identité, voix et manière de converser
                │    repères personnels stables
                │    règles du tour et date du jour
                │    la scène (ou, à défaut, un point de départ)
                │    extraits exacts des jours précédents
                │    récit d'un épisode passé pertinent, s'il y en a un
                │    agenda et faits récents pas encore dans la scène
                │    souvenirs déjà ouverts
                │    12 souvenirs suggérés par l'index
                │    mode d'emploi des outils de l'hôte
                │
                ├─ derniers messages exacts (jusqu'à 24)
                ├─ rappel final : décider, vérifier, ne pas bluffer
                └─ votre message
                        ↓
                    MODÈLE DE L'HÔTE (LLM)
                        │
                        ├─ il en sait assez → il répond
                        ├─ il lui faut un souvenir → daemon_read_souvenirs
                        ├─ il lui faut une parole passée → daemon_recall_l0
                        └─ il lui faut autre chose → un outil de l'hôte
                                ↓
                            retour au même modèle
                        ↓
                    RÉPONSE VISIBLE
                        │
                        ├──→ copie exacte dans L0
                        └──→ file d'après-tour
                                │
                                ├─ Radar, pour chaque message
                                ├─ Temporal Curator si une date compte
                                ├─ MemoryAgent si un fait mérite d'être retenu
                                ├─ relance éventuelle à programmer
                                └─ nouvelle scène
```

Rien ne s'intercale entre votre message et le modèle de l'hôte : pas de
programme qui choisit la réponse à sa place, pas de second modèle qui la
réécrit, pas d'appel au modèle avant le premier mot. La préparation ne fait
que lire des fichiers et les assembler ; le travail coûteux vient après la
réponse.

## Avant la réponse : ce que le modèle lit

**Qui parle.** Le nom et le genre grammatical du daemon sont choisis à
l'installation. Sa personnalité (`persona.md`) et sa manière de converser
(`conversation.md`) sont des fichiers que vous pouvez modifier.

**Ce qui reste vrai.** `context.md` garde quelques repères personnels stables
qui accompagnent chaque échange.

**Ce qui se passe maintenant.** La scène décrit la situation présente.
Lorsqu'il n'y en a pas encore, par exemple au premier message, un point de
départ plus simple la remplace : le contexte en cours, l'agenda et l'état de
la conversation, présentés comme datés et révisables. L'agenda et les faits
arrivés depuis la dernière scène sont ajoutés à côté d'elle, et ils l'emportent
en cas de désaccord.

**Ce qui a été dit.** Les 24 derniers messages sont repris mot pour mot depuis
L0, avec de courts extraits exacts des jours précédents. Sur OpenClaw, qui
garde son propre historique, le daemon ajoute seulement les messages qui y
manquent.

**Ce dont on peut se souvenir.** L'index propose au plus douze souvenirs liés
au sujet. Ceux que la scène juge déjà utiles sont joints en entier. Si
l'index trouve aussi le récit d'un épisode passé écrit par REM, ce récit est
joint comme un souvenir du passé, jamais comme une description du présent.

**Comment trancher.** Juste avant votre message, un rappel demande au modèle
de considérer toute la situation avant de décider, d'ouvrir une preuve
lorsqu'un fait lui manque, et de ne jamais faire semblant de connaître ce
qu'il n'a pas vérifié.

## Pendant la réponse : deux outils mémoire

Le daemon ajoute deux outils à ceux de l'hôte :

- `daemon_read_souvenirs` ouvre jusqu'à huit souvenirs en entier ;
- `daemon_recall_l0` retrouve une parole exacte dans L0, quand les souvenirs
  ne suffisent pas.

Ces deux outils remplacent la mémoire propre à l'hôte, dont les outils sont
masqués. Sur Hermes, un outil d'extraction de pages Web sans moteur configuré
est aussi masqué, car il échouerait à chaque appel. Tous les autres outils
sont ceux de l'hôte, quels qu'ils soient : ce que le modèle peut chercher ou
faire au-delà de la mémoire dépend de l'hôte et de sa configuration, pas du
daemon, qui ne les limite ni ne les ordonne. Il pose seulement une règle : un
résultat vide, bloqué ou ambigu ne prouve rien.

## Après la réponse : l'entretien

Chaque message rejoint la file d'après-tour. Le worker la traite dans l'ordre,
même si l'hôte redémarre entre-temps.

```text
pour chaque message, dans l'ordre
  → Radar : que demande cet échange ?
  → Temporal Curator : met l'agenda à jour si une date compte
  → MemoryAgent : crée ou complète des souvenirs si nécessaire

après une série de messages rapprochés
  → relance éventuelle à programmer
  → une seule nouvelle scène qui couvre tous ces messages
  → index des souvenirs mis à jour s'ils ont changé
```

**Le Radar** est le premier jugement, fait pour chaque message. Il ne répond
jamais à l'utilisateur et n'écrit rien lui-même : comme un radar balaie le
terrain pour signaler ce qui demande de l'attention, il relit l'échange pour
décider quel entretien lancer. Un modèle reçoit le message dans son contexte :
les messages récents, l'agenda, le contexte en cours, les repères stables et
la carte des souvenirs existants (leurs noms et leurs sujets, sans leur
contenu). Il répond notamment à trois questions, et chaque réponse commande
la suite :

- **L'échange apporte-t-il quelque chose à retenir ?** S'il est léger, comme
  un simple accord, un remerciement ou la suite d'un fil sans rien de nouveau,
  MemoryAgent ne tourne pas. Sinon, MemoryAgent est lancé.
- **Quels souvenirs existants sont concernés ?** Le Radar les désigne sur la
  carte, et MemoryAgent les relit en entier avant de les compléter ou de les
  corriger, au lieu d'en créer un doublon.
- **Une date, une échéance ou une routine est-elle apportée ou corrigée ?**
  Si oui, Temporal Curator met l'agenda à jour.

Ainsi, seul le travail utile est fait, et chaque écriture part d'une lecture
de tout l'échange plutôt que de mots isolés.

Une **relance** est un message que le daemon peut envoyer plus tard de
lui-même, par exemple pour prendre des nouvelles après un rendez-vous. Le
daemon décide si et quand ; l'hôte programme l'envoi et le délivre.

Les faits sont toujours traités message par message. Seule la scène, qui est
entièrement réécrite à chaque fois, est calculée une seule fois pour une
série de messages. L'ancienne scène reste en place tant que la nouvelle n'est
pas prête.

Chaque fichier a un seul responsable. MemoryAgent écrit les souvenirs, mais
ni l'agenda, ni `conversation.md`, ni L0. Temporal Curator est le seul à
écrire l'agenda. Un nettoyage automatique range les échéances dépassées dans
une archive, sans affirmer qu'elles ont eu lieu.

## La nuit : Dreaming et REM

**Dreaming** relit les souvenirs écrits pendant les dernières 48 heures, les
compare aux messages exacts dont ils viennent, et corrige ou fusionne les
doublons avec prudence. Il ne touche pas à l'agenda.

**REM** écrit le récit d'épisodes passés et le range dans `Archives/Episodes`.
L'index peut retrouver ces récits, mais ils restent datés : leurs
interprétations ne deviennent jamais des faits actuels.

## Où vit la mémoire

```text
<dossier mémoire>                 à ouvrir dans Obsidian
├── Projects/ Areas/ Resources/   les souvenirs, rangés en cinq dossiers
├── Garden/ Archives/             (REM écrit dans Archives/Episodes)
└── .daemon/                      fonctionnement interne, à ne pas supprimer
    ├── core/daemon_identity.json nom et genre du daemon
    ├── levels/L0_ground_zero/    L0 : l'historique exact des échanges
    └── users/<id>/               context.md, agenda.md, contexte en cours,
                                  scène et état de la conversation
```

Les cinq dossiers suivent une méthode de classement appelée **PARGA** :
*Projects* (projets en cours), *Areas* (responsabilités, personnes et
situations suivies dans la durée), *Resources* (ce qui mérite d'être gardé
pour plus tard), *Garden* (idées et enseignements qui peuvent mûrir) et
*Archives* (ce qui est terminé, toujours consultable).
[Obsidian](https://obsidian.md) est une application de prise de notes qui
ouvre directement ce dossier. [PARGA : le classement de la mémoire](parga.fr.md)
détaille chaque dossier, l'anatomie d'un souvenir et les méthodes dont PARGA
s'inspire.

Companion, l'application qui installe le daemon, crée ce dossier pour chaque
daemon et affiche son emplacement. Les fichiers Markdown font autorité.
L'index, ses embeddings et la scène peuvent être recalculés à partir d'eux :
ils aident à trouver et à comprendre, mais ne prouvent rien.

## Qui fait foi

| Élément | Ce qu'il prouve |
| --- | --- |
| Votre message actuel | Ce que vous affirmez ou demandez maintenant. Il l'emporte toujours. |
| L0 | Ce qui a exactement été dit. |
| Souvenir ouvert | Un fait personnel durable, dans sa portée. |
| `agenda.md` | Un engagement ou une échéance que vous avez confiés, pas le fait qu'ils aient eu lieu. |
| Outil de l'hôte | Ce que son résultat contient, au moment où il a été obtenu. |
| Scène | Une lecture de la situation, sourcée et révisable. |
| Souvenirs suggérés par l'index | Rien : ce sont des pistes à ouvrir. |
| Réponse précédente du daemon | Seulement ce que le daemon a dit, sauf si vous l'avez confirmé. |

## Si quelque chose manque

- Si la préparation échoue, l'hôte répond quand même, avec son propre
  contexte et la consigne de n'affirmer aucun fait personnel absent du
  dialogue visible. La réponse commence alors par un avis indiquant que la
  mémoire personnelle était indisponible.
- Sans scène, le modèle répond depuis le point de départ simple décrit plus
  haut.
- Si l'index ne répond pas, la liste complète des souvenirs le remplace, et
  l'incident est noté dans les journaux.
- Si l'entretien échoue, le travail reste dans la file, en attente ou en échec
  avec son diagnostic, et peut être repris. La conversation n'est ni bloquée,
  ni interrompue par un message technique.

## Pour aller plus loin

- [Architecture](architecture.fr.md) : chaque composant, chaque étape d'un
  tour et chaque règle, en détail.
- [Référence de la mémoire](memory_structure_developer_guide.md) : formats,
  provenance et cycle de vie des souvenirs.
- [Configuration](configuration.md) (en anglais) : emplacements, identité et
  réglages.
- [Glossaire](glossary.md) (en anglais) : les autres termes du projet.
