# PARGA : Le Classement De La Mémoire

[English](parga.md) · **Français**

La mémoire d'un daemon est un dossier de notes Markdown (un format de texte
simple) sur votre ordinateur. Chaque note est un **souvenir** : elle garde ce
que le daemon sait d'un sujet durable de votre vie, comme une personne, un
projet, une préférence, une situation ou une idée, avec son histoire et
l'origine de chaque fait.

**PARGA** est la façon dont ces souvenirs sont rangés. Le nom reprend
l'initiale des cinq dossiers : **Projects**, **Areas**, **Resources**,
**Garden** et **Archives**. Quatre viennent de la méthode PARA de Tiago Forte.
Le cinquième, Garden, est le jardin de vos idées, inspiré des principes du
Zettelkasten.

## Pourquoi un classement

Un historique de conversation permet de retrouver des paroles, mais oblige à
reconstituer leur histoire à chaque fois. À l'inverse, une liste de petits
faits isolés perd ce qui les relie : l'objectif, les tentatives, les personnes
concernées, la raison d'un blocage, le dernier état confirmé.

Un souvenir rassemble donc toute la continuité d'un sujet. La note d'une
réparation garde la panne, les essais qui ont échoué, la pièce attendue et la
dernière nouvelle de l'atelier. Quand vous en reparlez, le daemon comprend
votre message à la lumière de cette histoire, sans avoir à relire toute sa
mémoire.

Le classement en dossiers répond à une autre question : où chercher, et
comment vous, vous retrouvez-vous dans votre propre mémoire. Les dossiers sont
de vrais dossiers, que vous pouvez parcourir, ouvrir et réorganiser, par
exemple dans [Obsidian](https://obsidian.md), une application de prise de notes
qui ouvre directement ce dossier.

## Les cinq dossiers

| Dossier | Ce qu'on y trouve | Exemple | À ne pas confondre |
| --- | --- | --- | --- |
| **Projects** | Un résultat que vous poursuivez et qui aura une fin : son périmètre, ses décisions, ce dont il dépend et son avancement. | Préparer un anniversaire ; déployer un outil dans une équipe. | Une suggestion du daemon n'est pas un engagement : elle ne crée pas de projet. |
| **Areas** | Ce qui dure sans date de fin : responsabilités et rôles, personnes et relations, préférences, situations que vous traversez. | Une collègue ; la santé ; une réparation en attente. | Une routine n'est pas un projet. |
| **Resources** | Une référence précise à utiliser plus tard, comme un livre, un podcast ou un guide, avec la raison pour laquelle vous la gardez. | Un podcast à écouter avant une négociation. | Sa présence ne prouve pas qu'il a été écouté ou lu. |
| **Garden** | Une idée, un principe ou un enseignement que vous avez formulé ou adopté, et qui pourrait servir dans une tout autre partie de votre vie. La note grandit chaque fois que vous revenez à l'idée. | « Essayer un changement sur un petit pilote réversible avant de le généraliser. » | Un fait de votre vie va dans le dossier du sujet concerné ; une référence (livre, article, vidéo) va dans `Resources` ; une idée suggérée par le daemon et que vous n'avez pas reprise n'est pas gardée. |
| **Archives** | Ce qui est terminé ou devenu inactif, avec son état final exact, toujours consultable. | Un voyage passé, avec ce qui a été décidé et vécu ; le récit d'un épisode passé. | Archivé ne veut pas dire réussi : une annulation, une pause et une réussite restent des états différents. |

Chaque dossier peut contenir des sous-dossiers, qui évoluent avec vos besoins
(`Areas/Work`, `Areas/Family`, `Projects/Home`…). Les noms de dossiers sont en
anglais ; le contenu des notes reste dans la langue de vos conversations.

## Garden : le jardin de vos idées

**Les autres dossiers rangent ce qui vous arrive ; Garden fait pousser ce que
vous pensez.**

`Projects`, `Areas`, `Resources` et `Archives` rangent votre vie : ce que vous
poursuivez, ce qui dure, ce que vous gardez pour plus tard, ce qui est
terminé. `Garden` range vos idées : un principe, une conviction, une méthode ou
une leçon tirée de l'expérience, que vous avez formulés ou adoptés.

Une idée n'appartient pas à un seul sujet. Vous rénovez votre cuisine et vous
apprenez qu'il vaut mieux tout mesurer deux fois avant de commander. Le
chantier va dans `Projects`, puis dans `Archives` une fois terminé. La leçon,
elle, va dans `Garden` : elle reste disponible pour un déménagement ou un autre
achat, longtemps après la fin du chantier. Rangée dans le projet, elle aurait
été archivée avec lui.

**La question à se poser.** Cette note pourrait-elle servir dans une tout
autre partie de votre vie ? Si oui, elle va dans `Garden`. Si elle décrit une
situation précise, elle va dans le dossier de cette situation.

### Pourquoi « jardin »

Un jardin n'est pas une boîte de rangement : ce qu'on y plante pousse. Chaque
étape de la vie d'une note Garden correspond à ce que fait le daemon.

| Au jardin | Dans Garden |
| --- | --- |
| On plante une graine. | Vous énoncez ou adoptez une idée. **MemoryAgent**, le jugement d'après-réponse qui entretient les souvenirs, crée une note qui dit ce qu'est l'idée, où elle s'applique, où elle cesse de s'appliquer et d'où elle vient dans la conversation. |
| Elle pousse. | Vous y revenez ou l'appliquez à une autre situation : la note est complétée. Son historique garde chaque étape, ce qui montre comment l'idée a mûri et où elle a été mise à l'épreuve. |
| On taille. | Vous la nuancez ou changez d'avis : la correction est notée, et l'ancienne version reste dans l'historique. |
| Les racines s'étendent. | La note est reliée aux projets et aux domaines où l'idée est née ou a servi, avec une phrase qui dit pourquoi. |
| On transplante. | L'idée devient elle-même un engagement, par exemple vous décidez de lancer le pilote qu'elle décrivait : la note passe dans `Projects`. Si elle inspire seulement un projet distinct, elle reste dans `Garden` et la note du nouveau projet y renvoie. |
| On ne plante que ses propres graines. | Une idée suggérée par le daemon et que vous n'avez pas reprise n'est pas gardée. |

Le jardin ne pousse pas tout seul : une note Garden évolue quand vous reparlez
de l'idée. Le daemon ne la retravaille pas de lui-même entre deux
conversations, et il n'invente pas d'idées pour remplir le dossier.

### Ce qui n'y va pas

- **Un fait de votre vie**, même important : il appartient au projet ou au
  domaine concerné. « Ma sœur compte énormément pour moi » décrit une relation et va dans
  `Areas` ; « une décision se prend mieux après une nuit de sommeil » est une
  idée qui peut servir partout et va dans `Garden`.
- **Une référence** (livre, article, vidéo) : elle va dans `Resources`. Une
  idée qui en vient et que vous adoptez peut avoir sa propre note Garden,
  reliée à cette référence.
- **Une idée du daemon** que vous n'avez pas reprise.

### Comment une note Garden est retrouvée

Comme tout souvenir, une note Garden est retrouvée par l'**index des
souvenirs**, un moteur de recherche local qui compare le sens de la
conversation à celui de chaque note, quel que soit son dossier. Un principe
adopté au travail peut donc revenir dans une conversation sur l'organisation
d'un événement familial. L'index ne fait que suggérer : le modèle ouvre la
note avant de s'y appuyer.

Garden est privé et n'est publié nulle part. En cela il diffère des *jardins
numériques* (*digital gardens*), des sites personnels où les notes sont
publiées au fil de leur croissance.

## Un sujet, une note, des liens

Chaque souvenir a **un seul emplacement**. Une personne impliquée dans deux
projets garde une seule note, dans `Areas`, et chaque projet la cite par un
lien. Les liens s'écrivent comme dans Obsidian, `[[Areas/Work/Nora]]`, suivis
d'une phrase qui dit pourquoi les deux notes sont liées. MemoryAgent ne relie
que des notes qui existent et n'ajoute pas de liens pour remplir un graphe.

Le nom d'un dossier ne prouve rien. Appartenir au même foyer ne veut pas dire
être présent aujourd'hui ; les goûts d'une personne restent attachés à cette
personne. Toute relation entre deux sujets est écrite dans une note et appuyée
sur ce que vous avez dit.

## L'anatomie d'un souvenir

Un souvenir commence par quelques propriétés, qui servent au daemon à le
retrouver, puis un texte organisé en sections. Voici une note de projet et la
note Garden à laquelle elle renvoie. Les noms de sections sont en anglais, le
texte dans la langue de la conversation.

````markdown
---
memory_id: theme/work/orion
kind: project
memory_areas:
  - work
subjects:
  - orion
facets:
  - rollout
aliases:
  - Pilote Orion
---
# Déploiement d'Orion

## Scope
Déployer Orion auprès de l'équipe support, en commençant par un pilote.

## Last confirmed state
Le 2026-01-12, le pilote est validé ; sa date de lancement reste à confirmer.

## History
- 2026-01-08 : un lancement global est envisagé.
- 2026-01-12 : l'équipe choisit un pilote pour limiter le risque de perturbation.

## Still unknown
- Date de lancement confirmée.

## Links
- [[Areas/Work/Nora]] : chargée du suivi du pilote.
- [[Garden/Work/Petits pilotes réversibles]] : le principe qui a fait choisir un pilote.
````

````markdown
---
memory_id: theme/work/small_reversible_pilots
kind: knowledge
memory_areas:
  - work
subjects:
  - pilots
facets:
  - risk
---
# Petits pilotes réversibles

## Scope
Un principe de travail adopté par l'utilisateur : essayer un changement sur un
petit périmètre réversible avant de le généraliser.

## History
- 2026-01-12 : adopté en choisissant le pilote d'Orion : « Désormais je
  commencerai toujours par un petit pilote qu'on peut annuler. »

## Still unknown
- S'il l'applique aussi en dehors du travail.

## Links
- [[Projects/Work/Déploiement d'Orion]] : là où le principe a été adopté.
````

| Propriété | Rôle |
| --- | --- |
| `memory_id` | L'identifiant du souvenir, fixé à sa création. Il ne change pas quand le fichier est renommé ou déplacé. |
| `kind` | La nature du sujet : projet, domaine, situation, personne, groupe, organisation, lieu, objet, ressource, connaissance ou épisode. |
| `memory_areas` | Les domaines de vie concernés, par exemple `work` ou `health`. |
| `subjects`, `facets` | Des étiquettes qui aident l'index à retrouver le souvenir. Elles ne prouvent aucune relation. |
| `aliases` | D'autres noms du même sujet. |

Les sections disent ce qui est connu et depuis quand : le **périmètre**
(Scope), le **dernier état confirmé** et sa date, l'**historique** des
étapes, ce qui **reste inconnu** et les **liens**. Une section n'apparaît que
si elle a un contenu. L'entretien complète l'historique au lieu de le
remplacer par un résumé : on doit toujours pouvoir comprendre comment on en
est arrivé là.

Les dates s'écrivent `AAAA-MM-JJ`, ou `AAAA-MM` et `AAAA` quand on n'en sait
pas plus. La date d'un événement, celle où il a été mentionné et celle où le
fichier a été modifié sont trois choses différentes.

Ces propriétés et ces liens sont ceux d'Obsidian : la mémoire s'y lit et s'y
modifie sans extension.

## Qui range, et comment un souvenir change de dossier

Après chaque échange, MemoryAgent décide s'il faut créer une note, en
compléter une ou la corriger. Quand il crée une note, il choisit son dossier.
Il ne crée pas une note par échange : beaucoup de messages n'apportent rien
de durable.

Un souvenir peut changer de dossier, mais seulement après un élément nouveau
de votre part qui le justifie. MemoryAgent cite alors vos mots exacts et
explique la raison du changement. Deux cas typiques :

- un projet que vous dites terminé ou abandonné passe dans `Archives` ;
- une idée de `Garden` devenue un engagement concret passe dans `Projects`.

Lors d'un déplacement, le `memory_id` ne change pas, et les liens que d'autres
notes font vers la note déplacée sont mis à jour vers son nouvel emplacement.
Rien n'est archivé parce qu'on n'en parle plus : le silence ne résout pas un
problème ouvert, et un souvenir archivé reste lisible et retrouvable.

Vous pouvez aussi ranger, renommer ou modifier vos notes vous-même. Le daemon
respecte l'emplacement choisi et retrouve la note par son `memory_id`.

## Pourquoi pas de dossier « à trier »

Beaucoup de méthodes de prise de notes commencent par une boîte de réception,
un dossier où l'on dépose tout avant de trier. PARGA n'en a pas, car rien ne
serait perdu sans elle :

- **L0**, le niveau brut de l'historique de conversation (*Level 0*, aussi
  appelé *Ground Zero*), garde chaque message mot pour mot ;
- les traitements d'entretien en attente restent dans une file jusqu'à leur
  réalisation, même après un redémarrage.

Une information ambiguë reste donc une question ouverte, et une panne reste
un travail à reprendre ; ni l'une ni l'autre ne devient un fait inventé pour
être rangé.

## D'où vient PARGA

| Ce que PARGA applique | Source | Comment |
| --- | --- | --- |
| Ranger selon l'usage plutôt que par thème | PARA, IPCRA | Les dossiers `Projects`, `Areas`, `Resources`, `Archives` |
| Les rôles comme porte d'entrée | IPCRA | Les rôles sont rangés dans `Areas` |
| Un jardin d'idées à côté des dossiers d'action | Eliott Meunier | `Garden` |
| Une idée par note, séparée de son origine | Zettelkasten | `Garden` |
| Des liens qui disent leur raison | Zettelkasten | Une phrase après chaque lien |
| Un identifiant indépendant du rangement | Zettelkasten | `memory_id`, inchangé par un renommage ou un déplacement |
| La source gardée avec la note | Zettelkasten | L'origine de chaque fait dans la conversation, avec sa date |

### PARA, de Tiago Forte

[PARA](https://fortelabs.com/blog/para/) range toute information en quatre
catégories, selon son utilité pour l'action (*organize by actionability*) :
les **projets**, efforts à court terme vers un objectif ; les **domaines**
(*areas*), parties du travail et de la vie qui demandent une attention
continue, comme la santé, les finances ou la maison ; les **ressources**,
sujets d'intérêt ; les **archives**, éléments devenus inactifs des trois
autres.

PARGA garde ce critère et ces quatre dossiers, avec trois différences :

- **Areas** accueille aussi des notes dont le sujet n'est pas une
  responsabilité : une personne, une relation, une préférence, une situation
  que vous traversez sans l'avoir choisie. La nature du sujet est une
  propriété (`kind`), pas un dossier.
- **Resources** accueille des références précises avec la raison de leur
  conservation, plutôt que des sujets d'intérêt.
- **Qui range.** Dans PARA, c'est vous qui rangez et qui jugez qu'une chose est
  devenue inactive. Dans PARGA, un assistant range après chaque échange et ne
  peut pas déduire d'un silence qu'une chose est finie : un déplacement exige
  une parole de votre part. C'est aussi pourquoi chaque note porte des dates et
  des sources.

### IPCRA et le Garden, d'Eliott Meunier

Dans [une vidéo sur l'organisation des connaissances personnelles](https://eliottmeunier.com/youtube-lzmxl3cfzn8/),
Eliott Meunier présente deux organisations complémentaires et la façon dont il
les combine.

**IPCRA** (*Inbox*, *Projets*, *Casquettes*, *Ressources*, *Archives*) trie par
utilité, pas par thème. Pour chaque information, la question est : dans quel
contexte voudrai-je retomber dessus ?

- tout arrive d'abord dans l'*Inbox* ;
- si l'information peut servir un projet actuel ou futur, elle va dans ce
  projet, où les idées s'accumulent jusqu'à sa réalisation ;
- sinon, si elle concerne l'un de vos rôles, les *casquettes* (au sens de
  « porter plusieurs casquettes »), par exemple vos proches, elle va avec ce
  rôle ;
- sinon, en dernier recours, elle va dans *Ressources*, rangées par thème :
  une connaissance inutilisable maintenant mais qui servira plus tard ;
- ce qui n'est plus actif va dans *Archives*.

Le **Zettelkasten**, qu'il recommande pour la créativité et la recherche,
découpe la connaissance en notes permanentes, une note par concept, reliées
par le contexte, par l'exemple et par la logique : une note en éclaire une
autre, l'illustre, la complète ou la contredit.

Il recommande d'utiliser les deux dans le même coffre Obsidian : IPCRA pour
les projets et les fichiers, et un **Garden** qui contient les notes
Zettelkasten.

PARGA fait la même combinaison. Il reprend la question d'IPCRA comme règle de
rangement, range les rôles dans `Areas` plutôt que dans un dossier à part, et
ne garde pas la boîte de réception, pour les raisons données plus haut. Son
`Garden` contient les idées, selon les principes du Zettelkasten décrits
ci-dessous.

### Zettelkasten, les principes de Garden

Un *Zettelkasten* (« boîte à fiches » en allemand) est une collection de notes
reliées entre elles. Son exemple le plus connu est celui du sociologue
allemand Niklas Luhmann (1927-1998), professeur à l'université de Bielefeld :
environ 90 000 fiches écrites du début des années 1950 à la fin des années
1990, aujourd'hui conservées et numérisées par le
[Niklas Luhmann-Archiv](https://niklas-luhmann-archiv.de/).
L'[introduction de Sascha Fast sur zettelkasten.de](https://zettelkasten.de/introduction/)
expose les principes de la méthode, notamment :

- l'**atomicité** : une note contient une seule unité de connaissance ;
- un **identifiant unique** par note, pour pouvoir la relier ; l'endroit où
  la note est placée compte moins que la possibilité d'y renvoyer ;
- des **liens dont la raison est écrite** : un lien non expliqué n'apprend
  rien à qui le relit ;
- un texte écrit **avec ses propres mots**, avec les **sources** à la fin ;
- des **notes de structure**, qui rassemblent des liens vers d'autres notes à
  la manière d'une table des matières.

`Garden` est l'endroit où PARGA applique ces principes. Une note Garden
contient une seule idée, détachée du projet d'où elle vient et reliée par une
phrase aux endroits où elle s'applique : c'est ainsi que le Zettelkasten
sépare une idée de sa source et permet de la réutiliser. Le `memory_id` joue
le rôle de l'identifiant : il ne dépend pas de l'emplacement de la note et
survit à un déplacement.

PARGA s'écarte de la méthode sur quatre points :

- **L'atomicité ne vaut que pour Garden.** Les autres dossiers gardent une
  note par sujet avec toute son histoire, car une histoire personnelle
  découpée en une note par fait perd sa continuité.
- **Les dossiers suivent PARA.** Un Zettelkasten s'appuie sur les liens et les
  notes de structure plus que sur une hiérarchie de catégories. PARGA garde
  des dossiers, parce qu'ils disent à quoi sert une note et aident à se
  retrouver dans Obsidian.
- **Celui qui écrit n'est pas celui qui pense.** Dans un Zettelkasten, vous
  écrivez vos propres pensées avec vos propres mots. Dans PARGA, c'est
  MemoryAgent qui écrit les notes. Ses règles remplacent cette discipline :
  seule une idée que vous avez formulée ou adoptée entre dans Garden, et la
  note indique d'où elle vient dans la conversation.
- **Ni notes de structure, ni numérotation de Luhmann.** Les portes d'entrée
  sont les dossiers, les propriétés (`subjects`, `facets`, `aliases`) et
  l'index des souvenirs.

## Pour aller plus loin

- [Le daemon en un coup d'œil](overview.fr.md) : comment la mémoire est lue
  avant chaque réponse et entretenue après.
- [Référence de la mémoire](memory_structure_developer_guide.md) : formats,
  provenance, protections d'écriture et code concerné.
- [Votre mémoire](memory.md) (en anglais) : ouvrir et gérer la mémoire dans
  Obsidian.
